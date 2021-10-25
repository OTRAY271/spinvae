"""
Neural networks classes for synth parameters regression, and related utility functions.

These regression models can be used on their own, or passed as constructor arguments to
extended AE models.
"""

from collections.abc import Iterable
from abc import ABC, abstractmethod  # Abstract Base Class

import torch.nn as nn

from nflows.transforms.base import CompositeTransform
from nflows.transforms.autoregressive import MaskedAffineAutoregressiveTransform
from nflows.transforms.permutations import ReversePermutation

from data.preset import PresetIndexesHelper
import model.base
import model.flows
from model.flows import CustomRealNVP, InverseFlow
import model.loss


class PresetActivation(nn.Module):
    """ Applies the appropriate activations (e.g. sigmoid, hardtanh, softmax, ...) to different neurons
    or groups of neurons of a given input layer. """
    def __init__(self, idx_helper: PresetIndexesHelper,
                 numerical_activation=nn.Hardtanh(min_val=0.0, max_val=1.0),
                 cat_softmax_activation=False):
        """
        :param idx_helper:
        :param numerical_activation: Should be nn.Hardtanh if numerical params often reach 0.0 and 1.0 GT values,
            or nn.Sigmoid to perform a smooth regression without extreme 0.0 and 1.0 values.
        :param cat_softmax_activation: if True, a softmax activation is applied on categorical sub-vectors.
            Otherwise, applies the same HardTanh for cat and num params (and softmax should be applied in loss function)
        """
        super().__init__()
        self.idx_helper = idx_helper
        self.numerical_act = numerical_activation
        self.cat_softmax_activation = cat_softmax_activation
        if self.cat_softmax_activation:
            self.categorical_act = nn.Softmax(dim=-1)  # Required for categorical cross-entropy loss
            # Pre-compute indexes lists (to use less CPU)
            self.num_indexes = self.idx_helper.get_numerical_learnable_indexes()
            self.cat_indexes = self.idx_helper.get_categorical_learnable_indexes()  # type: Iterable[Iterable]
        else:
            pass  # Nothing to init....

    def forward(self, x):
        """ Applies per-parameter output activations using the PresetIndexesHelper attribute of this instance. """
        if self.cat_softmax_activation:
            x[:, self.num_indexes] = self.numerical_act(x[:, self.num_indexes])
            for cat_learnable_indexes in self.cat_indexes:  # type: Iterable
                x[:, cat_learnable_indexes] = self.categorical_act(x[:, cat_learnable_indexes])
        else:  # Same activation on num and cat ('one-hot encoded') params
            x = self.numerical_act(x)
        return x


# TODO class to "reverse" preset softmax activations.
#    could be done by using the properly one-hot encoded sub-vectors, by applying a simple affine functions
#    (whose coeffs will depend on the size of the one-hot sub-vector, to always get the same softmax activation)


class RegressionModel(model.base.TrainableModel, ABC):
    def __init__(self, architecture: str, dim_z: int, idx_helper: PresetIndexesHelper, cat_softmax_activation=False,
                 model_config=None, train_config=None):
        super().__init__(train_config=train_config, model_type='reg')
        self.architecture = architecture
        self.arch_args = architecture.split('_')  # Split between base args and opt args (e.g. _nobn)
        self.dim_z = dim_z
        self.idx_helper = idx_helper

        self.activation_layer = PresetActivation(self.idx_helper, cat_softmax_activation=cat_softmax_activation)

        # Attributes used for training only (losses, ...
        if train_config is not None and model_config is not None:
            self.dropout_p = train_config.reg_fc_dropout
            if train_config.params_cat_bceloss and model_config.params_reg_softmax:
                raise AssertionError("BCE loss requires no-softmax at reg model output")
            self.backprop_criterion = model.loss.\
                SynthParamsLoss(self.idx_helper, train_config.normalize_losses,
                                cat_bce=train_config.params_cat_bceloss,
                                cat_softmax=(not model_config.params_reg_softmax
                                             and not train_config.params_cat_bceloss),
                                cat_softmax_t=train_config.params_cat_softmax_temperature)
            # Monitoring losses always remain the same
            self.num_eval_criterion = model.loss.\
                QuantizedNumericalParamsLoss(self.idx_helper, loss_type='L1')
            self.accuracy_criterion = model.loss.\
                CategoricalParamsAccuracy(self.idx_helper, reduce=True, percentage_output=True)
        else:
            self.controls_criterion, self.num_eval_criterion, self.accuracy_criterion = None, None, None
            self.dropout_p = 0.0

    @abstractmethod
    def _reg_model_without_activation(self, z):
        pass

    def forward(self, z):
        """ Applies the regression model to a z latent vector (VAE latent flow output samples). """
        return self.activation_layer(self.reg_model(z))


class MLPControlsRegression(RegressionModel):
    def __init__(self, architecture: str, dim_z: int, idx_helper: PresetIndexesHelper, cat_softmax_activation=False,
                 model_config=None, train_config=None):
        """
        :param architecture: MLP automatically built from architecture string. E.g. '3l1024' means
            3 hidden layers of 1024 neurons. Some options can be given after an underscore
            (e.g. '3l1024_nobn' adds the no batch norm argument). See implementation for more details.  TODO implement
        :param dim_z: Size of a z_K latent vector
        :param idx_helper:
        """
        super().__init__(architecture, dim_z, idx_helper, cat_softmax_activation, model_config, train_config)
        if len(self.arch_args) == 1:
            num_hidden_layers, num_hidden_neurons = self.arch_args[0].split('l')
            num_hidden_layers, num_hidden_neurons = int(num_hidden_layers), int(num_hidden_neurons)
        else:
            raise NotImplementedError("Arch suffix arguments not implemented yet")
        # Layers definition
        self.reg_model = nn.Sequential()
        for l in range(0, num_hidden_layers):
            if l == 0:
                self.reg_model.add_module('fc{}'.format(l + 1), nn.Linear(dim_z, num_hidden_neurons))
            else:
                self.reg_model.add_module('fc{}'.format(l + 1), nn.Linear(num_hidden_neurons, num_hidden_neurons))
            # No BN or dropouts in the 2 last FC layers
            # Dropout in the deepest hidden layers is absolutely necessary (strong overfit otherwise).
            if l < (num_hidden_layers - 1):
                self.reg_model.add_module('bn{}'.format(l + 1), nn.BatchNorm1d(num_features=num_hidden_neurons))
                self.reg_model.add_module('drp{}'.format(l + 1), nn.Dropout(self.dropout_p))
            self.reg_model.add_module('act{}'.format(l + 1), nn.ReLU())
        self.reg_model.add_module('fc{}'.format(num_hidden_layers + 1), nn.Linear(num_hidden_neurons,
                                                                                  self.idx_helper.learnable_preset_size))

    def _reg_model_without_activation(self, z):
        return self.reg_model(z)


class FlowControlsRegression(RegressionModel):
    def __init__(self, architecture: str, dim_z: int, idx_helper: PresetIndexesHelper,
                 fast_forward_flow=True, cat_softmax_activation=False,
                 model_config=None, train_config=None):
        """
        :param architecture: Flow automatically built from architecture string. E.g. 'realnvp_16l200' means
            16 RealNVP flow layers with 200 hidden features each. Some options can be given after an underscore
            (e.g. '16l200_bn' adds batch norm). See implementation for more details.  TODO implement suffix options
        :param dim_z: Size of a z_K latent vector, which is also the output size for this invertible normalizing flow.
        :param idx_helper:
        :param dropout_p:
        :param fast_forward_flow: If True, the flow transform will be built such that it is fast (and memory-efficient)
            in the forward direction (else, it will be fast in the inverse direction). Moreover, if batch-norm is used
            between layers, the flow can be trained only its 'fast' direction (which can be forward or inverse
            depending on this argument).
        """
        super().__init__(architecture, dim_z, idx_helper, cat_softmax_activation, model_config, train_config)
        self._fast_forward_flow = fast_forward_flow
        self.flow_type, self.num_flow_layers, self.num_flow_hidden_features, _, _, _ = \
            model.flows.parse_flow_args(architecture, authorize_options=False)
        # Default: BN usage everywhere but between the 2 last layers
        self.bn_between_flows = True
        self.bn_within_flows = True
        self.bn_output = False

        # Multi-layer flow definition
        if self.flow_type.lower() == 'realnvp' or self.flow_type.lower() == 'rnvp':
            # RealNVP - custom (without useless gaussian base distribution) and no BN on last layers
            self._forward_flow_transform = CustomRealNVP(
                self.dim_z, self.num_flow_hidden_features, self.num_flow_layers, dropout_probability=self.dropout_p,
                bn_between_layers=self.bn_between_flows, bn_within_layers=self.bn_within_flows,
                output_bn=self.bn_output)
        elif self.flow_type.lower() == 'maf':
            transforms = []
            for l in range(self.num_flow_layers):
                transforms.append(ReversePermutation(features=self.dim_z))
                # TODO Batch norm added on all flow MLPs but the 2 last
                #     and dropout p
                transforms.append(MaskedAffineAutoregressiveTransform(features=self.dim_z,
                                                                      hidden_features=self.num_flow_hidden_features,
                                                                      use_batch_norm=False,  # TODO (l < num_layers-2),
                                                                      dropout_probability=0.5  # TODO as param
                                                                      ))
            self._forward_flow_transform = CompositeTransform(transforms)  # Fast forward  # TODO rename
            # The inversed MAF flow should never (cannot...) be used during training:
            #   - much slower than forward (in nflows implementation)
            #   - very unstable
            #   - needs ** huge ** amounts of GPU RAM
        else:
            raise ValueError("Undefined flow type '{}'".format(self.flow_type))

    def _reg_model_without_activation(self, z):
        v_out, _ = self.flow_forward_function(z)
        return v_out

    @property
    def is_flow_fast_forward(self):  # TODO improve, real nvp is fast forward and inverse...
        return self._fast_forward_flow

    @property
    def flow_forward_function(self):
        if self._fast_forward_flow:
            return self._forward_flow_transform.forward
        else:
            return self._forward_flow_transform.inverse

    @property
    def flow_inverse_function(self):
        if not self._fast_forward_flow:
            return self._forward_flow_transform.forward
        else:
            return self._forward_flow_transform.inverse




