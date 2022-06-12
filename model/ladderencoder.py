from typing import Dict, Tuple

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchinfo

from model.convlayer import ConvBlock2D, DownsamplingResBlock


class LadderEncoder(nn.Module):

    def __init__(self, conv_arch, latent_arch, latent_levels: int, input_tensor_size: Tuple[int, int, int, int],
                 approx_dim_z=2000):
        """
        Contains cell which define the hierarchy levels (the output of each cell is used to extract latent values)
            Each cell is made of blocks (skip connection may be added/concat/other at the end of block)
                Each block contains one or more conv, act and norm layers
         TODO also encode optional preset

        :param conv_arch:
        :param latent_arch:
        :param latent_levels:
        :param input_tensor_size:
        :param approx_dim_z:
        """

        super().__init__()
        self.single_ch_conv_arch = conv_arch
        self.latent_arch = latent_arch
        self._input_tensor_size = input_tensor_size
        self._single_ch_input_size = (1, 1, input_tensor_size[2], input_tensor_size[3])
        self._num_input_ch = input_tensor_size[1]

        # - - - - - 1) Build the single-channel CNN (applied to each input audio channel) - - - - -
        conv_args = self.single_ch_conv_arch['args']
        n_blocks = self.single_ch_conv_arch['n_blocks']
        if self.single_ch_conv_arch['name'].startswith('specladder'):
            assert n_blocks == 8  # 9 layers would allow to set dim_z exactly but is not implemented yet
            if conv_args['adain'] or conv_args['att']:
                raise NotImplementedError()
            self.single_ch_cells = list()
            if latent_levels == 1:
                cells_last_block = [n_blocks - 1]  # Unique cell ends with the very last block
            elif latent_levels == 2:
                cells_last_block = [4, n_blocks - 1]
            elif latent_levels == 3:
                cells_last_block = [3, 5, n_blocks - 1]
            elif latent_levels == 4:
                cells_last_block = [2, 3, 5, n_blocks - 1]
            else:
                raise NotImplementedError("Cannot build encoder with {} latent levels".format(latent_levels))
            self.single_ch_cells.append(nn.Sequential())

            for i_blk in range(n_blocks):
                residuals_path = nn.Sequential()
                blk_in_ch = 2**(i_blk+2)  # number of input channels
                blk_out_ch = 2**(i_blk+3)  # number of channels increases after each strided conv (at the block's end)
                if conv_args['big']:
                    blk_in_ch, blk_out_ch = blk_in_ch * 2, blk_out_ch * 2
                min_ch = 1 if not conv_args['bigger'] else 128
                max_ch = 512 if not conv_args['bigger'] else 1024
                blk_in_ch, blk_out_ch = np.clip([blk_in_ch, blk_out_ch], min_ch, max_ch)
                blk_hid_ch = blk_in_ch  # base number of internal (hidden) block channels
                if i_blk == 0:
                    blk_in_ch, blk_hid_ch = 1, 1
                    kernel_size = (5, 5)
                else:
                    kernel_size = (4, 4)

                n_layers = self.single_ch_conv_arch['n_layers_per_block'] if i_blk > 0 else 1
                if n_layers >= 2:
                    raise NotImplementedError()
                if n_layers >= 1:
                    conv = nn.Conv2d(blk_hid_ch, blk_out_ch, kernel_size, (2, 2), 2)
                    act = nn.LeakyReLU(0.1) if i_blk > 0 else None
                    norm = nn.BatchNorm2d(blk_hid_ch) if i_blk > 0 else None
                    residuals_path.add_module('strided', ConvBlock2D(conv, act, norm, 'nac'))

                # Add a skip-connection if required, then add this new block to the current cell
                if conv_args['res'] and i_blk > 0:
                    current_block = DownsamplingResBlock(residuals_path)
                else:
                    current_block = residuals_path  # No skip-connection
                self.single_ch_cells[-1].add_module('blk{}'.format(i_blk), current_block)
                if i_blk in cells_last_block and i_blk < (n_blocks - 1):  # Start building the next cell
                    self.single_ch_cells.append(nn.Sequential())

        else:  # Spectrograms only are supported at the moment
            raise NotImplementedError("Unimplemented '{}' architecture".format(self.single_ch_conv_arch['name']))

        # - - - - - 2) Latent inference networks (1 / cell) - - - - -
        self.latent_cells = list()
        # retrieve output size of each CNN cell and approximate dim_z for each dimension
        self.cells_output_shapes = self._get_cells_output_shapes()

        # Conv alone: for a given level, feature maps from all input spectrograms are merged using a conv network only
        # and the output is latent distributions parameters mu and sigma^2. Should be a very simple and fast network
        if self.latent_arch['name'].startswith("conv"):
            # Latent space size: number of channels chosen such that the total num of latent coordinates
            # is close the approx_dim_z_per_level value (these convolutions keep feature maps' H and W)
            n_latent_ch_per_level = self._get_conv_latent_ch_per_level(approx_dim_z)

            if self.latent_arch['name'] == 'convk11':
                kernel_size, padding = (1, 1), (0, 0)
            elif self.latent_arch['name'] == 'convk33':
                kernel_size, padding = (3, 3), (1, 1)
            else:
                raise NotImplementedError("Cannot build latent arch {}: name not implement".format(latent_arch))
            for i, cell_output_shape in enumerate(self.cells_output_shapes):
                n_latent_ch = n_latent_ch_per_level[i] * 2  # Output mu and sigma2
                n_input_ch = input_tensor_size[1] * cell_output_shape[1]
                if self.latent_arch['n_layers'] == 1:
                    self.latent_cells.append(nn.Conv2d(n_input_ch, n_latent_ch, kernel_size, 1, padding))
                elif self.latent_arch['n_layers'] == 2:  # No batch-norm inside the latent conv arch
                    n_intermediate_ch = int(round(np.sqrt(n_input_ch * n_latent_ch)))
                    self.latent_cells.append(nn.Sequential(
                        nn.Conv2d(n_input_ch, n_intermediate_ch, kernel_size, 1, padding),
                        nn.LeakyReLU(0.1),
                        nn.Conv2d(n_intermediate_ch, n_latent_ch, kernel_size, 1, padding)
                    ))
                else:
                    raise ValueError("Convolutional arch. for latent vector computation must contain <= 2 layers.")
        else:
            raise NotImplementedError("Cannot build latent arch {}: name not implemented".format(latent_arch))

        # Finally, Python lists must be converted to nn.ModuleList to be properly recognized by PyTorch
        self.single_ch_cells = nn.ModuleList(self.single_ch_cells)
        self.latent_cells = nn.ModuleList(self.latent_cells)

    def _get_conv_latent_ch_per_level(self, approx_dim_z: int):
        """ Computes the number of output latent channels for each level, such that TODO DOC """
        latent_ch_per_level = list()
        # Build a linearly decreasing number of latent coords for the most hidden layers, even if the
        # feature maps' sizes decreases quadratically - not to favor too much the shallowest feature maps, which
        # could (seems to) lead to overfitting
        dim_z_ratios = np.arange(len(self.single_ch_cells), 0, -1)
        dim_z_ratios = dim_z_ratios / dim_z_ratios.sum()
        remaining_z_dims = approx_dim_z  # Number of latent dims yet to be assigned to a latent level
        # Assign the exact number of channels, level-by-level, to get as close as possible to the requested dim_z
        for latent_lvl, conv_cell_output_shape in enumerate(self.cells_output_shapes):  # Enc: latent_lvl == cell_index
            num_values_per_ch = np.prod(conv_cell_output_shape[2:])
            if latent_lvl < (len(self.cells_output_shapes) - 1):  # For all latent levels but the deepest one
                num_ch = round(approx_dim_z * dim_z_ratios[latent_lvl] / num_values_per_ch)
                # Update the ratios using the actually obtained ratio for this level
                current_z_dims = num_ch * num_values_per_ch
                dim_z_ratios[latent_lvl] = current_z_dims / approx_dim_z
                sum_set_ratios = np.sum(dim_z_ratios[0:latent_lvl+1])
                sum_free_ratios = np.sum(dim_z_ratios[latent_lvl+1:])
                dim_z_ratios[latent_lvl+1:] = dim_z_ratios[latent_lvl+1:] * (1 - sum_set_ratios) / sum_free_ratios
                remaining_z_dims -= current_z_dims
            else:  # use remaining_z_dims for the deepest latent level (smallest feature maps, easier adjustements)
                num_ch = round(remaining_z_dims / num_values_per_ch)
            if num_ch < 1:
                raise ValueError(
                    "Approximate requested dim_z is too low (cell {} output shape{}). Please increase the requested"
                    " latent dimension (current: {}).".format(latent_lvl, conv_cell_output_shape, approx_dim_z))
            latent_ch_per_level.append(num_ch)
        return latent_ch_per_level

    def forward(self, x):
        """ Returns (z_mu, z_var): lists of length latent_levels """
        # 1) Apply single-channel CNN to all input channels
        latent_cells_input_tensors = [[] for _ in self.latent_cells]  # 1st dim: latent level ; 2nd dim: input ch
        for ch in range(self._input_tensor_size[1]):  # Apply all cells to a channel
            cell_x = torch.unsqueeze(x[:, ch, :, :], dim=1)
            for latent_level, cell in enumerate(self.single_ch_cells):
                cell_x = cell(cell_x)
                latent_cells_input_tensors[latent_level].append(cell_x)
        # Latent levels are currently independent (no top-down conditional posterior or prior)
        # We just concat inputs from all input channels
        latent_cells_input_tensors = [torch.cat(t, dim=1) for t in latent_cells_input_tensors]
        # 2) Compute latent vectors: tuple (mean and variance) of lists (one tensor per latent level)
        z_mu, z_var = list(), list()
        for latent_level, latent_cell in enumerate(self.latent_cells):
            z_out = latent_cell(latent_cells_input_tensors[latent_level])
            n_ch = z_out.shape[1]
            z_mu.append(z_out[:, 0:n_ch//2, :, :])
            z_var.append(F.softplus(z_out[:, n_ch//2:, :, :]))
        return z_mu, z_var

    @property
    def _summary_col_names(self):
        return "input_size", "kernel_size", "output_size", "num_params", "mult_adds"

    def get_single_ch_conv_summary(self):
        """ Torchinfo summary of the CNN that is sequentially applied to each input spectrogram. Does not
         include the output to extract latent vectors. """
        single_ch_cnn_without_latent = nn.Sequential()
        for i, cell in enumerate(self.single_ch_cells):
            single_ch_cnn_without_latent.add_module('cell{}'.format(i), cell)
        with torch.no_grad():
            return torchinfo.summary(
                single_ch_cnn_without_latent, input_size=self._single_ch_input_size,
                depth=5, verbose=0, device=torch.device('cpu'),
                col_names=self._summary_col_names, row_settings=("depth", "var_names")
            )

    def get_latent_cells_summaries(self):
        summaries = dict()
        for i, latent_cell in enumerate(self.latent_cells):
            latent_cell_input_shape = list(self.cells_output_shapes[i])
            latent_cell_input_shape[1] *= self._input_tensor_size[1]
            with torch.no_grad():
                summaries['latent_cell_{}'.format(i)] = torchinfo.summary(
                    latent_cell, input_size=latent_cell_input_shape,
                    depth=5, verbose=0, device=torch.device('cpu'),
                    col_names=self._summary_col_names, row_settings=("depth", "var_names")
                )
        return summaries

    def _get_cells_output_shapes(self):
        x = torch.zeros(self._single_ch_input_size)
        output_sizes = list()
        with torch.no_grad():
            for cell in self.single_ch_cells:
                x = cell(x)
                output_sizes.append(x.shape)
        return output_sizes
