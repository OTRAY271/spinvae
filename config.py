"""
Allows easy modification of all configuration parameters required to define,
train or evaluate a model.
This script is not intended to be run, it only describes parameters.
However, some dynamic hyper-parameters are properly set when this module is imported.

This configuration is used when running train.py as main.
When running train_queue.py, configuration changes are relative to this config.py file.

When a run starts, this file is stored as a config.json file. To ensure easy restoration of
parameters, please only use simple types such as string, ints, floats, tuples (no lists) and dicts.
"""


import datetime
# The config_confidential.py must be created by the user - required global attributes are described later in this file
from utils import config_confidential


# ===================================================================================================================
# ================================================= Model configuration =============================================
# ===================================================================================================================
class ModelConfig:
    def __init__(self):
        # ----------------------------------------------- Data ---------------------------------------------------
        self.data_root_path = config_confidential.data_root_path
        self.logs_root_dir = "saved"  # Path from this directory
        self.name = "z_loss"  # experiment base name
        self.run_name = 'dummy_embds_01'  # experiment run: different hyperparams, optimizer, etc... for a given exp
        # TODO anonymous automatic relative path
        self.pretrained_VAE_checkpoint = "/home/gwendal/Jupyter/nn-synth-interp/saved/" \
                                          "VAE_MMD_5020/presets_x4__enc_big_dec3resblk__batch64/checkpoints/00399.tar"
        self.allow_erase_run = True  # If True, a previous run with identical name will be erased before training
        # Comet.ml logger (replaces Tensorboard)
        self.comet_api_key = config_confidential.comet_api_key
        self.comet_project_name = config_confidential.comet_project_name
        self.comet_workspace = config_confidential.comet_workspace
        self.comet_experiment_key = 'xxxxxxxx'  # Will be set by cometwriter.py after experiment has been created
        self.comet_tags = []

        # ---------------------------------------- General Architecture --------------------------------------------
        # See model/encoder.py to view available architectures. Decoder architecture will be as symmetric as possible.
        # 'speccnn8l1' used for the DAFx paper (based on 4x4 kernels, square-shaped deep feature maps)
        # 'sprescnn': Spectral Res-CNN (based on 1x1->3x3->1x1 res conv blocks)
        # Arch args:
        #    '_adain' some BN layers are replaced by AdaIN (fed with a style vector w, dim_w < dim_z)
        #    '_att' self-attention in deep conv layers  TODO encoder and decoder
        #    '_big' (small improvements but +50% GPU RAM usage),   '_bigger'
        #    '_res' residual connections (blocks of 2 conv layer)
        #    '_time+' increases time resolution in the deepest layers
        self.encoder_architecture = 'speccnn8l1_res'
        self.attention_gamma = 1.0  # Amount of self-attention added to (some) usual convolutional outputs
        # Style network architecture: to get a style vector w from a sampled latent vector z0 (inspired by StyleGAN)
        # must be an mlp, but the number of layers and output normalization (_outputbn) can be configured
        # e.g. 8l1024: 8 layers, 1024 units per layer
        self.style_architecture = 'mlp_2l128_outputbn'  # batch norm layers are always added inside the mlp
        # Possible values: 'flow_realnvp_6l300', 'mlp_3l1024', ... (configurable numbers of layers and neurons)
        # TODO random permutations when building flows
        # 3l600 is associated to bad MMD values and "dirac-like" posteriors.
        # Maybe try a bigger flow to prevent a to strong constraint on the latent space?
        self.params_regression_architecture = 'flow_realnvp_8l500'  # TODO try bigger flow (if does not overfit anymore)
        self.params_reg_hardtanh_out = False  # Applies to categorical params (numerical are always hardtanh-activated)
        self.params_reg_softmax = False  # Apply softmax at the end of the reg model itself?
        # If True, loss compares v_out and v_in. If False, we will flow-invert v_in to get loss in the q_Z0 domain.
        # This option has implications on the regression model itself (the flow will be used in direct or inverse order)
        self.forward_controls_loss = True  # Must be true for non-invertible MLP regression (False is now deprecated)

        # --------------------------------------------- Latent space -----------------------------------------------
        # If True, encoder output is reduced by 2 for 1 MIDI pitch and 1 velocity to be concat to the latent vector
        self.concat_midi_to_z = None  # See update_dynamic_config_params()
        # Latent space dimension  ********* When using a Flow regressor, this dim is automatically set *************
        self.dim_z = 512  # Including possibly concatenated midi pitch and velocity
        # Latent flow architecture, e.g. 'realnvp_4l200' (4 flows, 200 hidden features per flow)
        #    - base architectures can be realnvp, maf, ...
        #    - set to None to disable latent space flow transforms: will build a BasicVAE or MMD-VAE
        #    - options: _BNinternal (batch norm between hidden MLPs, to compute transform coefficients),
        #               _BNbetween (between flow layers), _BNoutput (BN on the last two layers, or not)
        self.latent_flow_arch = None
        # self.latent_flow_arch = 'realnvp_6l300_BNinternal_BNbetween'

        # ------------------------------------------------ Audio -------------------------------------------------
        # Spectrogram size cannot easily be modified - all CNN decoders should be re-written
        self.note_duration = (3.0, 1.0)
        self.sampling_rate = 16000  # 16000 for NSynth dataset compatibility
        self.stft_args = (512, 256)  # fft size and hop size
        self.mel_bins = -1  # -1 disables Mel-scale spectrogram. Try: 257, 513, ...
        # Spectrogram sizes @ 22.05 kHz:
        #   (513, 433): audio 5.0s, fft size 1024, fft hop 256
        #   (257, 347): audio 4.0s, fft size 512 (or fft 1024 w/ mel_bins 257), fft hop 256
        #   (513, 347): audio 4.0s, fft size 1024 (no mel), fft hop 256
        # Sizes @ 16 kHz:
        #   (257, 251): audio 4.0s, fft size 512 (or fft 1024 w/ mel_bins 257), fft hop 256
        self.spectrogram_size = (257, 251)  # H x W. see data/dataset.py to retrieve this from audio/stft params
        self.mel_f_limits = (0, 8000)  # min/max Mel-spectrogram frequencies (librosa default 0:Fs/2)
        # All notes that must be available for each instrument (even if we currently use only a subset of those notes)
        self.required_dataset_midi_notes = ((41, 75), (48, 75), (56, 75), (63, 75), (56, 25), (56, 127))
        # Tuple of (pitch, velocity) tuples. Using only 1 midi note is fine.
        # self.midi_notes = ((56, 75), )  # Reference note: G#3 , intensity 75/127
        self.midi_notes = self.required_dataset_midi_notes
        self.stack_spectrograms = True  # If True, dataset will feed multi-channel spectrograms to the encoder
        self.stack_specs_features_mix_level = -2  # -1 corresponds to the deepest 1x1 conv, -2 to the layer before, ...
        # If True, each preset is presented several times per epoch (nb of train epochs must be reduced) such that the
        # dataset size is increased (6x bigger with 6 MIDI notes) -> warmup and patience epochs must be scaled
        self.increased_dataset_size = None  # See update_dynamic_config_params()
        self.spectrogram_min_dB = -120.0
        self.input_tensor_size = None  # see update_dynamic_config_params()

        # ---------------------------------- Synth (not used during pre-training) ----------------------------------
        self.synth = 'dexed'
        # Dexed-specific auto rename: '*' in 'al*_op*_lab*' will be replaced by the actual algos, operators and labels
        self.synth_args_str = 'al*_op*_lab*'  # Auto-generated string (see end of script)
        self.synth_params_count = -1  # Will be set automatically - see data.build.get_full_and_split_datasets
        self.learnable_params_tensor_length = -1  # Will be auto-set - see data.build.get_full_and_split_datasets
        # Modeling of synth controls probability distributions
        # Possible values: None, 'vst_cat', 'all<=xx' where xx is numerical params threshold cardinal, or 'all'
        self.synth_vst_params_learned_as_categorical = 'all'
        self.continuous_params_max_resolution = 50  # resolution of continuous synth params will be reduced to this
        # flags/values to describe the dataset to be used
        self.dataset_labels = None  # tuple of labels (e.g. ('harmonic', 'percussive')), or None to use all labels
        # Dexed: Preset Algorithms, and activated Operators (Lists of ints, None to use all)
        # Limited algorithms (non-symmetrical only): [1, 2, 7, 8, 9, 14, 28, 3, 4, 11, 16, 18]
        # Other synth: ...?
        self.dataset_synth_args = (None, [1, 2, 3, 4, 5, 6])
        # Directory for saving metrics, samples, models, etc... see README.md


# ===================================================================================================================
# ======================================= Training procedure configuration ==========================================
# ===================================================================================================================
class TrainConfig:
    def __init__(self):
        self.pretrain_ae_only = True  # Should we pre-train the auto-encoder model only?
        self.start_datetime = datetime.datetime.now().isoformat()
        # 128: faster train but lower higher MMD (more posterior collapse). 64: better MMD perf
        self.minibatch_size = 256
        self.main_cuda_device_idx = 0  # CUDA device for nonparallel operations (losses, ...)
        self.test_holdout_proportion = 0.1  # This can be reduced without mixing the train and test subsets
        self.k_folds = 9  # 10% for validation set, 80% for training
        self.current_k_fold = 0
        self.start_epoch = 0  # 0 means a restart (previous data erased). If > 0: will load start_epoch-1 checkpoint
        # Total number of epochs (including previous training epochs).  275 for StepLR regression model training
        self.n_epochs = 400 if self.pretrain_ae_only else 275  # See update_dynamic_config_params().
        # The max ratio between the number of items from each synth/instrument used for each training epoch (e.g. Dexed
        # has more than 30x more instruments than NSynth). All available data will always be used for validation.
        self.pretrain_synths_max_imbalance_ratio = 10.0  # Set to -1 to disable the weighted sampler.
        self.attention_gamma_warmup_period = 50

        # ------------------------------------------------ Losses -------------------------------------------------
        # Reconstruction loss: 'MSE' corresponds to free-mean, fixed-variance per-pixel Gaussian prob distributions.
        # TODO 'WeightedMSE' allows to give a higher loss to some parts of spectrograms (e.g. attach, low freqs, ??)
        self.reconstruction_loss = 'MSE'
        # Latent regularization loss: 'Dkl' or 'MMD' for Basic VAE, 'logprob' or 'MMD' loss with flow-VAE
        # 'MMD_determ_enc' also available: use a deterministic encoder
        self.latent_loss = 'Dkl'
        self.mmd_compensation_factor = 5.0  # Factor applied to MMD backprop losses only
        self.mmd_num_estimates = 1  # Number of MMD estimates per batch (maybe increase if small batch size)
        # Losses normalization allow to get losses in the same order of magnitude, but does not optimize the true ELBO.
        # When un-normalized, the reconstruction loss (log-probability of a multivariate gaussian) is orders of
        # magnitude bigger than other losses. Must remain True to ease convergence (too big recons loss)
        self.normalize_losses = True  # Normalize reconstruction and regression losses over their dimension
        # To compare different latent sizes, Dkl or MMD losses are not normalized such that each latent
        # coordinate always 'has' the same amount of regularization
        self.normalize_latent_loss = False
        # Here, beta = beta_vae / Dx in the beta-VAE formulation (ICLR 2017)
        # where Dx is the input dimensionality (257 * 251 = 64 507)
        # E.g. here: beta = 1 corresponds to beta_VAE = 6.5 e+4
        #            ELBO loss is obtained by using beta = 1.55 e-5
        self.beta = 1.6e-4
        self.beta_start_value = self.beta / 2.0  # Should not be zero (risk of a very unstable training)
        # Epochs of warmup increase from start_value to beta
        self.beta_warmup_epochs = 25  # See update_dynamic_config_params(). Used during pre-train only
        # - - - Synth parameters losses - - -
        # - General options
        self.params_model_additional_regularization = None  # 'inverse_log_prob' available for Flow-based models
        self.params_loss_compensation_factor = 1.0  # because MSE loss of the VAE is much lower (approx. 1e-2)
        self.params_loss_exclude_useless = True  # if True, sets to the 0.0 the loss related to 0-volume oscillators
        self.params_loss_with_permutations = False  # Backprop loss only; monitoring losses always use True
        # - Loss for a dense dequantized output loss (set to 'None' to activate other losses)
        self.params_dense_dequantized_loss = 'None'  # Preempts CE losses
        # - Cross-Entropy loss (deactivated when using dequantized outputs)
        # TODO log the more important as hparams into comet.ml
        self.params_cat_CE_label_smoothing = 0.0  # torch.nn.CrossEntropyLoss: label smoothing since PyTorch 1.10
        self.params_target_noise = 0.00
        self.params_cat_CE_use_weights = False
        self.params_cat_bceloss = False  # If True, disables the CE loss to compute BCE loss instead (deprecated)
        # FIXME Temperature if softmax if applied in the loss only (!=0 is deprecated)
        self.params_cat_softmax_temperature = 1.0

        # ------------------------------------------- Optimizer + scheduler -------------------------------------------
        # Different optimizer parameters can be used for the pre-trained AE and the regression networks
        # (see below: 'ae' or 'reg' prefixes or dict keys)
        self.optimizer = 'Adam'
        self.adam_betas = (0.9, 0.999)  # default (0.9, 0.999)
        # Maximal learning rate (reached after warmup, then reduced on plateaus)
        # LR decreased if non-normalized losses (which are expected to be 9e4 times bigger with a 257x347 spectrogram)
        # e-9 LR with e+4 (non-normalized) loss does not allow any train (vanishing grad?)
        self.initial_learning_rate = {'ae': 8e-5, 'reg': 1e-4}  # FIXME reset to 1e-4
        self.initial_ae_lr_factor_after_pretrain = 1e-1  # AE LR reduced after pre-train
        # Learning rate warmup (see https://arxiv.org/abs/1706.02677). Same warmup period for all schedulers.
        # The warmup will be must faster during pre-train  (See update_dynamic_config_params())
        self.lr_warmup_epochs = 20
        self.lr_warmup_start_factor = 0.05  # Reduced for large realnvp flows
        self.scheduler_name = 'StepLR'  # can use ReduceLROnPlateau during pre-train (stable CNN), StepLR for reg model
        self.enable_ae_scheduler_after_pretrain = False
        self.scheduler_lr_factor = {'ae': 0.4, 'reg': 0.2}
        # - - - StepLR scheduler options - - -
        self.scheduler_period = 50  # Will be increased during pre-train
        # - - - ReduceLROnPlateau scheduler options - - -
        # Possible values: 'VAELoss' (total), 'ReconsLoss', 'Controls/BackpropLoss', ... Losses will be summed
        self.scheduler_losses = {'ae': ('ReconsLoss/Backprop', ), 'reg': ('Controls/BackpropLoss', )}
        # Set a longer patience with smaller datasets and quite unstable trains
        # See update_dynamic_config_params(). 16k samples dataset:  set to 10
        self.scheduler_patience = {'ae': 25, 'reg': 15}
        self.scheduler_cooldown = {'ae': 25, 'reg': 15}
        self.scheduler_threshold = 1e-4
        # Training considered "dead" when dynamic LR reaches this ratio of a the initial LR
        # Early stop is currently used for the regression loss only, for the 'ReduceLROnPlateau' scheduler only.
        self.early_stop_lr_ratio = {'ae': 1e-10, 'reg': 1e-4}  # early stop not implemented for the ae model
        self.early_stop_lr_threshold = None  # See update_dynamic_config_params()

        # -------------------------------------------- Regularization -----------------------------------------------
        # WD definitely helps for regularization but significantly impairs results. 1e-4 seems to be a good compromise
        # for both Basic and MMD VAEs (without regression net). 3e-6 allows for the lowest reconstruction error.
        self.weight_decay = 1e-5
        self.ae_fc_dropout = 0.0  # 0.3 without MMD, to try to help prevent VAE posterior collapse
        self.reg_fc_dropout = 0.4
        self.latent_input_dropout = 0.0  # Should always remain zero... intended for tests (not tensorboard-logged)
        # When using a latent flow z0-->zK, z0 is not regularized. To keep values around 0.0, batch-norm or a 0.1Dkl
        # can be used (warning: latent input batch-norm is a very strong constraint for the network).
        # 'BN' (on encoder output), 'Dkl' (on q_Z0 gaussian flow input) or 'None' (always use a str arg)
        self.latent_flow_input_regularization = 'None'
        self.latent_flow_input_regul_weight = 0.1  # Used for 'Dkl' only

        # -------------------------------------------- Logs, figures, ... ---------------------------------------------
        self.save_period = 500  # Period for checkpoint saves (large disk size)
        self.plot_period = 20   # Period (in epochs) for plotting graphs into Tensorboard (quite CPU and SSD expensive)
        self.plot_epoch_0 = True
        self.verbosity = 1  # 0: no console output --> 3: fully-detailed per-batch console output
        self.init_security_pause = 0.0  # Short pause before erasing an existing run
        # Number of logged audio and spectrograms for a given epoch
        self.logged_samples_count = 4  # See update_dynamic_config_params()

        # -------------------------------------- Performance and Profiling ------------------------------------------
        self.dataloader_pin_memory = False
        self.dataloader_persistent_workers = True
        self.profiler_enabled = False
        self.profiler_epoch_to_record = 0  # The profiler will record a few minibatches of this given epoch
        self.profiler_kwargs = {'record_shapes': True, 'with_stack': True}
        self.profiler_schedule_kwargs = {'skip_first': 5, 'wait': 1, 'warmup': 1, 'active': 3, 'repeat': 2}





def update_dynamic_config_params(model_config: ModelConfig, train_config: TrainConfig):
    """ This function must be called before using any train attribute """

    # TODO perform config coherence checks in this function

    if train_config.pretrain_ae_only:
        model_config.comet_tags.append('pretrain')
        model_config.params_regression_architecture = 'None'
        train_config.lr_warmup_epochs = train_config.lr_warmup_epochs // 2
        train_config.lr_warmup_start_factor *= 2
        train_config.scheduler_period += train_config.scheduler_period // 2
    else:
        train_config.initial_learning_rate['ae'] *= train_config.initial_ae_lr_factor_after_pretrain
        train_config.beta_warmup_epochs = 0
        train_config.attention_gamma_warmup_period = 0

    # stack_spectrograms must be False for 1-note datasets - security check
    model_config.stack_spectrograms = model_config.stack_spectrograms and (len(model_config.midi_notes) > 1)
    # Artificially increased data size?
    model_config.increased_dataset_size = (len(model_config.midi_notes) > 1) and not model_config.stack_spectrograms
    model_config.concat_midi_to_z = (len(model_config.midi_notes) > 1) and not model_config.stack_spectrograms
    # Mini-batch size can be smaller for the last mini-batches and/or during evaluation
    model_config.input_tensor_size = \
        (train_config.minibatch_size, 1 if not model_config.stack_spectrograms else len(model_config.midi_notes),
         model_config.spectrogram_size[0], model_config.spectrogram_size[1])

    # Dynamic train hyper-params
    train_config.early_stop_lr_threshold = {k: train_config.initial_learning_rate[k] * ratio
                                            for k, ratio in train_config.early_stop_lr_ratio.items()}
    train_config.logged_samples_count = max(train_config.logged_samples_count, len(model_config.midi_notes))
    # Train hyper-params (epochs counts) that should be increased when using a subset of the dataset
    if model_config.dataset_synth_args[0] is not None:  # Limited Dexed algorithms?  TODO handle non-dexed synth
        train_config.n_epochs = 700
        train_config.lr_warmup_epochs = 10
        train_config.scheduler_patience = 10
        train_config.scheduler_cooldown = 10
        train_config.beta_warmup_epochs = 40
    # Train hyper-params (epochs counts) that should be reduced with artificially increased datasets
    # Augmented  datasets introduce 6x more backprops <=> 6x more epochs. Patience and cooldown must however remain >= 2
    if model_config.increased_dataset_size:  # Stacked spectrogram do not increase the dataset size (number of items)
        # FIXME handle the dicts
        N = len(model_config.midi_notes) - 1  # reduce a bit less that dataset's size increase
        train_config.n_epochs = 1 + train_config.n_epochs // N
        train_config.lr_warmup_epochs = 1 + train_config.lr_warmup_epochs // N
        train_config.scheduler_patience = 1 + train_config.scheduler_patience // N
        train_config.scheduler_cooldown = 1 + train_config.scheduler_cooldown // N
        train_config.beta_warmup_epochs = 1 + train_config.beta_warmup_epochs // N

    # Automatic model.synth string update - to summarize this info into 1 Tensorboard string hparam
    if model_config.synth == "dexed":
        if model_config.dataset_synth_args[0] is not None:  # Algorithms
            model_config.synth_args_str = model_config.synth_args_str.replace(
                "al*", "al" + '.'.join([str(alg) for alg in model_config.dataset_synth_args[0]]))
        if model_config.dataset_synth_args[1] is not None:  # Operators
            model_config.synth_args_str = model_config.synth_args_str.replace(
                "_op*", "_op" + ''.join([str(op) for op in model_config.dataset_synth_args[1]]))
        if model_config.dataset_labels is not None:  # Labels
            model_config.synth_args_str = model_config.synth_args_str.replace(
                "_lab*", '_' + '_'.join([label[0:4] for label in model_config.dataset_labels]))
    else:
        raise NotImplementedError("Unknown synth prefix for model.synth '{}'".format(model_config.synth))

