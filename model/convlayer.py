
"""
Defines some basic layer Classes to be integrated into bigger networks
"""
import copy
from typing import Optional, Tuple
from abc import ABC, abstractmethod

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F



class AdaIN(nn.Module):
    def __init__(self, num_style_features, num_conv_ch):
        """ Adaptive Instance Normalization, inspired by the StyleGAN generator architecture. """
        super().__init__()
        self.norm = nn.InstanceNorm2d(num_conv_ch, affine=False)  # No learnable params
        # Affine transforms (through basic affine FC layers) to compute normalization bias and scale
        self.bias_nn = nn.Linear(num_style_features, num_conv_ch)
        self.scale_nn = nn.Linear(num_style_features, num_conv_ch)

    def forward(self, x, w_style):
        """ Normalizes and rescales/re-biases the 2D feature maps x using style vectors w. """
        x = self.norm(x)
        b, s = self.bias_nn(w_style), self.scale_nn(w_style)
        # x shape : N x C x H x W  (W: time ; H: frequency)
        # w shape : N x C
        b = torch.unsqueeze(torch.unsqueeze(b, 2), 3).expand(-1, -1, x.shape[2], x.shape[3])
        s = torch.unsqueeze(torch.unsqueeze(s, 2), 3).expand(-1, -1, x.shape[2], x.shape[3])
        return x * s + b


class ActAndNorm(nn.Module):
    def __init__(self, in_ch, out_ch, act=nn.ReLU(),
                 norm_layer: Optional[str] = 'bn', adain_num_style_features: Optional[int] = None, reverse_order=False):
        """
        Base class for any conv layer with act and norm (usable a base class for Conv or Transpose Conv layers,
        or as main class). The forward function returns a batch of features maps, and a batch of w conditioning vectors.

        :param norm_layer: 'bn' (Batch Norm), 'adain' (Adaptive Instance Norm, requires w_style during forward) or None
        :param reverse_order: Ff False, conv->act->norm. If True, norm->act->conv (res blocks)
        """
        super().__init__()
        self.reverse_order = reverse_order
        self.conv = None  # can be assigned by child class - otherwise, not applied
        self.act = act
        self.norm_layer_description = norm_layer
        if self.norm_layer_description is None or self.norm_layer_description == 'None':
            self.norm = None
        elif self.norm_layer_description == 'bn':
            self.norm = nn.BatchNorm2d(out_ch if not reverse_order else in_ch)
        elif self.norm_layer_description == 'adain':
            self.norm = AdaIN(adain_num_style_features, out_ch if not reverse_order else in_ch)
        else:
            raise AssertionError("Layer norm {} not available.".format(self.norm_layer_description))

    def forward(self, x_and_w_style):
        """ Always returns a tuple (x, w) for the style to be passed to the next layer in the sequence. """
        x, w_style = x_and_w_style[0], x_and_w_style[1]
        if not self.reverse_order:  # Usual order: conv->act->norm
            if self.conv is not None:
                x = self.conv(x)
            x = self.act(x)
        if self.norm_layer_description == 'bn':
            x = self.norm(x)
        elif self.norm_layer_description == 'adain':
            if w_style is None:
                raise AssertionError("w_style cannot be None when using AdaIN")
            x = self.norm(x, w_style)
        else:
            pass  # Norm layer disabled (other possibly wrong values raised error in the ctor)
        if self.reverse_order:  # reversed order: norm->act->conv (used in res blocks)
            x = self.act(x)
            if self.conv is not None:
                x = self.conv(x)
        return x, w_style

    def set_attention_gamma(self, gamma):
        if isinstance(self.conv, nn.Sequential):
            self.conv[1].gamma = gamma  # Self-attention is always after the usual 'local' conv


class SelfAttentionConv2D(nn.Module):
    def __init__(self, n_ch, internal_n_ch: Optional[int] = None):
        """ Based on Self-Attention GAN (SAGAN, ICML19) """
        super().__init__()
        self._gamma = 0.1  # FIXME
        self.C = n_ch
        self.Cint = ((self.C // 8) if internal_n_ch is None else internal_n_ch)
        # Key, Query and Value matrices implemented using 1x1 1D convolutions (flattened input) without bias
        self.Wq = nn.Conv1d(self.C, self.Cint, kernel_size=(1,), bias=False)
        self.Wk = nn.Conv1d(self.C, self.Cint, kernel_size=(1,), bias=False)
        self.Wv = nn.Conv1d(self.C, self.Cint, kernel_size=(1,), bias=False)
        self.W_out_v = nn.Conv1d(self.Cint, self.C, kernel_size=(1,), bias=False)

    @property
    def gamma(self):
        return self._gamma

    @gamma.setter
    def gamma(self, v):
        self._gamma = v

    def forward(self, x):
        # SAGAN paper: Feature maps are represented as 2D (with C input channels) in Fig.2,
        # but input features are described as flattened to CxN (1D, not CxWxH 2D data) in the paper itself
        N = x.shape[2] * x.shape[3]
        x_flat = x.view(x.shape[0], x.shape[1], N)  # compatible strides, shared memory
        # Query, key and value shape: Cint x N     where N = W*H
        query, key, value = self.Wq(x_flat), self.Wk(x_flat), self.Wv(x_flat)
        # Attention output size: N x N
        # Different from the transformer:
        #     - QT.K to compute similarity between pixels (instead of Q.KT)
        #     - Compute a softmax on each column (such that sum of row (dim=1) values is 1.0), not on each row
        #     - no scaling before softmax.  TODO TRY SCALING (/ sqrt(N))
        att = F.softmax(torch.bmm(query.transpose(1, 2), key) / np.sqrt(N), dim=1)
        att_output = torch.bmm(value, att)
        # Increase number of channels before adding to the input
        return x + self.W_out_v(att_output).view(x.shape) * self.gamma  # TODO use contiguous after final reshape ?


class Conv2D(ActAndNorm):
    """ A basic conv layer with activation and normalization layers """
    def __init__(self, in_ch, out_ch, kernel_size, stride, padding, dilation=(1, 1),
                 padding_mode='zeros', act=nn.ReLU(),
                 norm_layer: Optional[str] = 'bn', adain_num_style_features: Optional[int] = None, reverse_order=False,
                 self_attention=False):
        super().__init__(in_ch, out_ch, act, norm_layer, adain_num_style_features, reverse_order)
        # Self-attention conv is computed after the usual local convolution, but we should rather
        #   consider that it's a parallel computation: self-attention values are added to local conv outputs
        local_conv = nn.Conv2d(in_ch, out_ch, kernel_size, stride, padding, dilation, padding_mode=padding_mode)
        self.conv = nn.Sequential(local_conv, SelfAttentionConv2D(out_ch)) if self_attention else local_conv


class TConv2D(ActAndNorm):
    """ A basic Transposed conv layer with activation and normalization layers """
    def __init__(self, in_ch, out_ch, kernel_size, stride, padding, output_padding=0, dilation=1, padding_mode='zeros',
                 act=nn.ReLU(), norm_layer: Optional[str] = 'bn',
                 adain_num_style_features: Optional[int] = None, reverse_order=False, self_attention=False):
        super().__init__(in_ch, out_ch, act, norm_layer, adain_num_style_features, reverse_order)
        local_conv = nn.ConvTranspose2d(in_ch, out_ch, kernel_size, stride, padding, output_padding,
                                        dilation=dilation, padding_mode=padding_mode)
        self.conv = nn.Sequential(local_conv, SelfAttentionConv2D(out_ch)) if self_attention else local_conv


class ResConv2DBase(nn.Module, ABC):
    """ Base class for ResConv2D and ResTConv2D: 2 convolutional layers with input added to the output
    (if necessary: number of channels adapted using a 1x1 conv).
    Final activation and norm are performed after the add operation.
    """
    def __init__(self, in_ch, out_ch, act=nn.ReLU(),
                 norm_layer: Optional[str] = 'bn', adain_num_style_features: Optional[int] = None):
        super().__init__()
        self.conv1_act_bn = None  # to be set by child class
        self.conv2 = None
        self.residuals_conv = nn.Conv2d(in_ch, out_ch, (1, 1)) if (in_ch != out_ch) else None
        self.act_norm_2 = ActAndNorm(in_ch, out_ch, copy.deepcopy(act),
                                     norm_layer, adain_num_style_features)

    @abstractmethod
    def _resize_x_skip(self, x_skip, output_feature_maps_size):
        pass

    def forward(self, x_and_w):
        x, w = x_and_w[0], x_and_w[1]  # conditioning w is always passed with x
        x_hidden, _ = self.conv1_act_bn((x, w))
        x_before_add = self.conv2(x_hidden)
        output_feature_maps_size = x_before_add.shape[2:4]
        # Skip connection - conv, automatic average pool if necessary
        x_skip = self.residuals_conv(x) if self.residuals_conv is not None else x
        if x_skip.shape[2:4] != output_feature_maps_size:
            x_skip = self._resize_x_skip(x_skip, output_feature_maps_size)
        return self.act_norm_2((x_before_add + x_skip, w))

    def set_attention_gamma(self, gamma):
        self.conv1_act_bn.set_attention_gamma(gamma)
        if isinstance(self.conv2, nn.Sequential):
            self.conv2[1].gamma = gamma   # Self-attention is always in 2nd position


class ResConv2D(ResConv2DBase):
    """ An automatic average pooling is performed on the residuals if necessary,
    in order to reduce the size of the feature maps."""
    def __init__(self, in_ch, hidden_ch, out_ch, kernel_size, stride, padding, dilation=(1, 1), padding_mode='zeros',
                 act=nn.ReLU(), norm_layer: Optional[str] = 'bn', adain_num_style_features: Optional[int] = None,
                 self_attention=(False, False)):
        super().__init__(in_ch, out_ch, act, norm_layer, adain_num_style_features)
        # Conv1 with act and norm - requires a style vector (tuple input/output)
        self.conv1_act_bn = Conv2D(
            in_ch, hidden_ch, kernel_size, stride, padding, dilation, padding_mode,
            act, norm_layer, adain_num_style_features, self_attention=self_attention[0])
        # Conv2 must have no norm and no activation (applied after residuals are summed)
        # Doesn't require a style vector (modules which inherit from nn.Module directly)
        conv2 = nn.Conv2d(hidden_ch, out_ch, kernel_size, stride, padding, dilation, padding_mode=padding_mode)
        self.conv2 = (nn.Sequential(conv2, SelfAttentionConv2D(out_ch)) if self_attention[1] else conv2)

    def _resize_x_skip(self, x_skip, output_feature_maps_size):
        return F.adaptive_avg_pool2d(x_skip, output_feature_maps_size)


class ResTConv2D(ResConv2DBase):
    """  An automatic upsampling bilinear interpolation is performed to increase the size of the feature maps. """
    def __init__(self, in_ch, hidden_ch, out_ch,
                 kernel_size, stride, padding, output_padding, dilation=(1, 1), padding_mode='zeros', act=nn.ReLU(),
                 norm_layer: Optional[str] = 'bn', adain_num_style_features: Optional[int] = None,
                 self_attention=(False, False)):
        if norm_layer == 'bn+adain' or norm_layer == 'adain+bn':
            norm1, norm2 = 'adain', 'bn'
        else:
            norm1, norm2 = norm_layer, norm_layer
        super().__init__(in_ch, out_ch, act, norm2, adain_num_style_features)
        self.conv1_act_bn = TConv2D(
            in_ch, hidden_ch, kernel_size, stride, padding, output_padding, dilation, padding_mode, act, norm1,
            adain_num_style_features, self_attention=self_attention[0])
        # No act and norm for conv2 (doesn't require an input style vector)
        conv2 = nn.ConvTranspose2d(hidden_ch, out_ch, kernel_size, stride, padding, output_padding,
                                   dilation=dilation, padding_mode=padding_mode)
        self.conv2 = (nn.Sequential(conv2, SelfAttentionConv2D(out_ch)) if self_attention[1] else conv2)

    def _resize_x_skip(self, x_skip, output_feature_maps_size):
        return F.interpolate(x_skip, output_feature_maps_size, mode='bilinear', align_corners=False)


class Upsampling2d(nn.Module):
    def __init__(self, scale_factor: Tuple[int, int], w_input_exists=True):
        """ Upsampling module that can be used inside a sequence of conv modules with conditioning vector w. """
        super().__init__()
        self.w_input_exists = w_input_exists
        self.scale_factor = scale_factor

    def forward(self, x_and_w):
        x, w = (x_and_w[0], x_and_w[1]) if self.w_input_exists else (x_and_w, None)
        output_feature_maps_size = list(x.shape[2:4])
        output_feature_maps_size[0] *= self.scale_factor[0]
        output_feature_maps_size[1] *= self.scale_factor[1]
        x = F.interpolate(x, output_feature_maps_size, mode='bilinear', align_corners=False)
        return (x, w) if self.w_input_exists else x


class ResBlock3Layers(nn.Module):
    def __init__(self, in_ch, internal_3x3_ch, out_ch, act=nn.ReLU(),
                 upsample=(False, False), downsample=(False, False), extra_padding=(0, 0),
                 norm_layer: Optional[str] = 'bn', adain_num_style_features: Optional[int] = None):
        """
        The reduced number of channels to perform the actual 3x3 conv is inspired by ResNeXt.

        :param extra_padding: Tuple of height and width padding (applied on both sides). The H x W ordering corresponds
            to Nbatch x C x H x W for PyTorch convolutional modules.
        :param norm_layer: If 'bn' or 'adain', is applied to all norm layers. If 'bn+adain', the middle residual
            layers uses AdaIN, the 2 others use BN.
        """
        super().__init__()
        self.in_ch, self.out_ch = in_ch, out_ch
        if norm_layer is None:
            bn, adain = False, False
        elif norm_layer.lower() == 'bn':
            bn, adain = True, False
        elif norm_layer.lower() == 'adain':
            bn, adain = False, True
        elif norm_layer.lower() == 'bn+adain' or norm_layer.lower() == 'adain+bn':
            bn, adain = True, True
        else:
            raise ValueError("'{}' norm layer: unrecognized string argument.".format(norm_layer))
        if adain and adain_num_style_features is None:
            raise AssertionError("AdaIN requires adain_num_style_features not to be None.")
        if (upsample[0] or upsample[1]) and (downsample[0] or downsample[1]):
            raise AssertionError("Cannot simultaneously upsample and downsample. Please check input args.")
        self.resize_module = nn.Sequential()
        # Residual network: reduces the nb of channels, applies a 3x3 conv, then increases the nb of ch before add
        self.res_convs = nn.Sequential()
        # allow a mix of adain and bn --> 1 adain first, all the rest is BN

        norm_name = 'bn' if bn else ('adain' if adain else None)  # 1st layer: BN has the priority over AdaIN
        self.res_convs.add_module('redu1x1',
                                  Conv2D(in_ch, internal_3x3_ch, (1, 1), (1, 1), (0, 0),  # kernel, stride, padding
                                         act=copy.deepcopy(act), reverse_order=True,
                                         norm_layer=norm_name, adain_num_style_features=adain_num_style_features))

        norm_name = 'adain' if adain else ('bn' if bn else None)  # 2nd layer: AdaIN has the priority over BN
        # Strided convolution (downsampling),
        # or bicubic upsampling (3x3 tconv leads to artifacts: https://distill.pub/2016/deconv-checkerboard/)
        conv_stride = (1, 1)
        conv_padding = (1 + extra_padding[0], 1 + extra_padding[1])
        if upsample[0] or upsample[1]:
            upsampling_size_factor = (2 if upsample[0] else 1, 2 if upsample[1] else 1)
            self.resize_module.add_module('upsmp', Upsampling2d(upsampling_size_factor, w_input_exists=False))
            self.res_convs.add_module('upsmp', Upsampling2d(upsampling_size_factor))
        # allow custom input padding (to increase size more). left/right, top/bottom
        if extra_padding != (0, 0) and not (downsample[0] or downsample[1]):  # extra padding
            self.resize_module.add_module('pad', nn.ZeroPad2d((extra_padding[1], extra_padding[1],
                                                               extra_padding[0], extra_padding[0])))
        if downsample[0] or downsample[1]:
            conv_stride = (2 if downsample[0] else 1, 2 if downsample[1] else 1)
            pool_padding = (conv_padding[0]-1 if downsample[0] else extra_padding[0],
                            conv_padding[1]-1 if downsample[1] else extra_padding[1])
            if pool_padding[0] > 0 or pool_padding[1] > 0:
                self.resize_module.add_module('pad', nn.ZeroPad2d((pool_padding[1], pool_padding[1],
                                                                   pool_padding[0], pool_padding[0])))
            self.resize_module.add_module('pool', nn.AvgPool2d(conv_stride, ceil_mode=True,
                                                               padding=(0, 0)))
                                                               #padding=(conv_stride[0] - 1, conv_stride[1] - 1)))
        self.res_convs.add_module('conv3x3',
                                  Conv2D(internal_3x3_ch, internal_3x3_ch, (3, 3), conv_stride, conv_padding,
                                         act=copy.deepcopy(act), reverse_order=True,
                                         norm_layer=norm_name, adain_num_style_features=adain_num_style_features))

        norm_name = 'bn' if bn else ('adain' if adain else None)  # 3rd layer: BN has the priority over AdaIN
        self.res_convs.add_module('incr1x1',
                                  Conv2D(internal_3x3_ch, out_ch, (1, 1), (1, 1), (0, 0),
                                         act=copy.deepcopy(act), reverse_order=True,
                                         norm_layer=norm_name, adain_num_style_features=adain_num_style_features))

        if len(self.resize_module) == 0:
            del self.resize_module
            self.resize_module = None

    def forward(self, x_and_w):
        """
        :param x_and_w: Tuple: Feature maps x, conditiong (style) vector w
        :return: Output feature maps, conditiong (style) vector w
        """
        x, w = x_and_w[0], x_and_w[1]
        res, _ = self.res_convs(x_and_w)

        # if upsampling AND decreasing ch count: we'll use the method inspired by "the devil is in the decoder"
        #  (https://arxiv.org/abs/1707.05847) -> bilinear (or bicubic) upsampling and add some channels (non-learnable)
        # Upsampling and decreasing ch count should happen simultaneously, but this is not a constraint

        # upsampling or downsampling (possibly padding also)
        if self.resize_module is not None:
            x = self.resize_module(x)

        # Addition of residuals (number of in/out channels can be different)
        if self.in_ch == self.out_ch:
            x = x + res
        elif self.in_ch < self.out_ch:  # Increasing number of channels (e.g. in an encoder)
            # The new channels (non-existent in original input x) will include residual values only
            res[:, 0:self.in_ch, :, :] = res[:, 0:self.in_ch, :, :] + x
            x = res
        elif self.in_ch > self.out_ch:  # Decreasing number of channels (e.g. in a decoder)
            # Split input channels into out_ch groups, add (and normalize) all channels from each group
            in_ch_split_indices = np.array_split(np.arange(0, self.in_ch), self.out_ch)
            for out_ch_index, in_ch_indices in enumerate(in_ch_split_indices):
                for in_ch_index in list(in_ch_indices):
                    res[:, out_ch_index, :, :] += (x[:, in_ch_index, :, :] / in_ch_indices.shape[0])
            x = res

        return x, w
