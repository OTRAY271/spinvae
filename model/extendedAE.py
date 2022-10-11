"""
Deprecated, see hierarchicalvae.py

Defines 'Extended Auto-Encoders', which are basically spectrogram VAEs with an additional neural network
which infers synth parameters values from latent space values.
"""

from typing import Optional

import torch
import torch.nn as nn

from model import VAE
import model.base
import model.regression
from data.preset import PresetIndexesHelper


class ExtendedAE(nn.Module):
    """ Model based on any compatible Auto-Encoder and Regression models. """

    def __init__(self, ae_model: nn.Module, reg_model: Optional[nn.Module] = None):
        super().__init__()
        self.ae_model = ae_model
        self.reg_model = reg_model

    @property
    def is_flow_based_latent_space(self):
        return self.ae_model.is_flow_based_latent_space

    @property
    def is_flow_based_regression(self):
        if isinstance(self.reg_model, model.regression.FlowControlsRegression):
            return True
        elif isinstance(self.reg_model, model.regression.MLPControlsRegression)\
                or isinstance(self.reg_model, model.base.DummyModel):
            return False
        else:
            raise TypeError("Unrecognized synth params regression model")

    def forward(self, x, sample_info=None):
        """
        Auto-encodes the input (does NOT perform synth parameters regression).
        This class (and its sub-models) must not store any temporary self.* tensor (e.g. loss computations),
        because it will be parallelized on multiple GPUs and output tensors will be concatenated by DataParallel.
        """
        return self.ae_model(x, sample_info)

    def latent_loss(self, z_0_mu_logvar, z_0_sampled, z_K_sampled, log_abs_det_jac):
        return self.ae_model.latent_loss(z_0_mu_logvar, z_0_sampled, z_K_sampled, log_abs_det_jac)

    def find_preset_inverse(self, x_in, sample_info, target_u_out):
        """ Returns the latent code z corresponding to the target output preset, as well as the corresponding
        accuracy and numerical error. """
        pass
