import copy
import multiprocessing
import threading
import os
import sys
import time
import shutil
import json
import datetime
import pathlib
import warnings
from typing import List, Optional

import numpy as np

import torch
import torchinfo

import humanize

import config
from data.abstractbasedataset import AudioDataset
import model.base
import model.VAE
import utils
import utils.figures
import utils.stat
from .tbwriter import TensorboardSummaryWriter  # Custom modified summary writer
from .cometwriter import CometWriter
import logs.logger_mp

_erase_security_time_s = 5.0


def get_model_run_directory(root_path, model_config):
    """ Returns the directory where saved models and config.json are stored, for a particular run.
    Does not check whether the directory exists or not (it must have been created by the RunLogger) """
    return root_path.joinpath(model_config.logs_root_dir)\
        .joinpath(model_config.name).joinpath(model_config.run_name)


def get_model_checkpoints_dir(root_path: pathlib.Path, model_config):
    return root_path.joinpath(model_config.logs_root_dir).joinpath(model_config.name)\
        .joinpath(model_config.run_name).joinpath('checkpoints')


def get_model_checkpoint_from_dir(checkpoints_dir: pathlib.Path, epoch, device=None):
    checkpoint_path = checkpoints_dir.joinpath('{:05d}.tar'.format(epoch))
    try:
        if device is None:
            checkpoint = torch.load(checkpoint_path)  # Load on original device
        else:
            checkpoint = torch.load(checkpoint_path, map_location=device)  # e.g. train on GPU, load on CPU
    except (OSError, IOError) as e:
        available_checkpoints = "Available checkpoints: {}".format([f.name for f in checkpoints_dir.glob('*.tar')])
        print(available_checkpoints)
        raise ValueError("Cannot load checkpoint for epoch {}: {}".format(epoch, e))
    return checkpoint


def get_model_checkpoint(root_path: pathlib.Path, model_config, epoch, device=None):
    """ Returns the path to a .tar saved checkpoint, or prints all available checkpoints and raises an exception
    if the required epoch has no saved .tar checkpoint. """
    checkpoints_dir = get_model_checkpoints_dir(root_path, model_config)
    return get_model_checkpoint_from_dir(checkpoints_dir, epoch, device)


def get_model_last_checkpoint(root_path: pathlib.Path, model_config, verbose=True, device=None):
    checkpoints_dir = root_path.joinpath(model_config.logs_root_dir).joinpath(model_config.name)\
        .joinpath(model_config.run_name).joinpath('checkpoints')
    available_epochs = [int(f.stem) for f in checkpoints_dir.glob('*.tar')]
    assert len(available_epochs) > 0  # At least 1 checkpoint should be available
    if verbose:
        print("Loading epoch {} from {}".format(max(available_epochs), checkpoints_dir))
    return get_model_checkpoint(root_path, model_config, max(available_epochs), device)





def get_tensorboard_run_directory(root_path, model_config):
    """ Returns the directory where Tensorboard model metrics are stored, for a particular run. """
    return root_path.joinpath(model_config.logs_root_dir).joinpath('runs')\
        .joinpath(model_config.name).joinpath(model_config.run_name)


def erase_run_data(root_path, model_config):
    """ Erases all previous data (Tensorboard, config, saved models) for a particular run of the model. """
    if _erase_security_time_s > 0.1:
        print("[RunLogger] *** WARNING *** '{}' run for model '{}' will be erased in {} seconds. "
              "Stop this program to cancel ***"
              .format(model_config.run_name, model_config.name, _erase_security_time_s))
        time.sleep(_erase_security_time_s)
    else:
        print("[RunLogger] '{}' run for model '{}' will be erased.".format(model_config.run_name, model_config.name))
    shutil.rmtree(get_model_run_directory(root_path, model_config))  # config and saved models
    shutil.rmtree(get_tensorboard_run_directory(root_path, model_config))  # tensorboard


class RunLogger:
    """ Class for saving interesting data during a training run:
     - graphs, losses, metrics, and some results to Comet or Tensorboard
     - config.py as a json file
     - trained models (checkpoints)

     See ../README.md to get more info on storage locations.
     """
    def __init__(self, root_path, model_config: config.ModelConfig, train_config: config.TrainConfig,
                 logger_type='comet', use_multiprocessing=True):
        """

        :param root_path: pathlib.Path of the project's root folder
        :param model_config: from config.py
        :param train_config: from config.py
        :param logger_type: 'comet', 'tensorboard' or 'comet|tensorboard' (tensorboard is deprecated, incomplete logs)
        """
        self.logger_type = logger_type.lower()
        if ('comet' not in self.logger_type) and ('tensorboard' not in self.logger_type):
            raise ValueError("Unsupported logger_type '{}'".format(logger_type))
        # Configs are stored but not modified by this class
        self.model_config = model_config
        self.train_config = train_config
        self.verbosity = train_config.verbosity
        global _erase_security_time_s  # Very dirty.... but quick
        _erase_security_time_s = train_config.init_security_pause
        assert train_config.start_epoch >= 0  # Cannot start from a negative epoch
        self.restart_from_checkpoint = (train_config.start_epoch > 0)
        self.current_epoch = -1  # Will be init later
        if self.restart_from_checkpoint:
            raise NotImplementedError("Cannot compute the current step after restarting from checkpoint")
        else:
            self.current_step = 0
        self.current_step = 0  # FIXME if restart
        # - - - - - Directories creation (if not exists) for model - - - - -
        self.log_dir = root_path.joinpath(model_config.logs_root_dir).joinpath(model_config.name)
        self._make_dirs_if_dont_exist(self.log_dir)
        # Tensorboard directory is always required for the PyTorch Profiler to write its results
        self.tensorboard_model_dir = root_path.joinpath(model_config.logs_root_dir)\
            .joinpath('runs').joinpath(model_config.name)
        self._make_dirs_if_dont_exist(self.tensorboard_model_dir)
        # - - - - - Run directories and data management - - - - -
        if self.train_config.verbosity >= 1:
            print("[RunLogger] Starting logging into '{}'".format(self.log_dir))
        self.run_dir = self.log_dir.joinpath(model_config.run_name)  # This is the run's reference folder
        self.checkpoints_dir = self.run_dir.joinpath('checkpoints')
        self.tensorboard_run_dir = self.tensorboard_model_dir.joinpath(model_config.run_name)  # Required for profiler
        # Check: does the run folder already exist?
        if not os.path.exists(self.run_dir):
            if train_config.start_epoch != 0:
                raise RuntimeError("config.py error: this new run must start from epoch 0")
            self._make_model_run_dirs()
            if self.train_config.verbosity >= 1:
                print("[RunLogger] Created '{}' directory to store config and models.".format(self.run_dir))
        # If run folder already exists
        else:
            if self.restart_from_checkpoint:
                if self.verbosity >= 1:
                    print("[RunLogger] Will load saved checkpoint (previous epoch: {})"
                          .format(self.train_config.start_epoch - 1))
            else:  # Start a new fresh training
                if not model_config.allow_erase_run:
                    raise RuntimeError("Config does not allow to erase the '{}' run for model '{}'"
                                       .format(model_config.run_name, model_config.name))
                else:
                    erase_run_data(root_path, model_config)  # module function
                    self._make_model_run_dirs()
        # - - - - - Epochs, Batches, ... - - - - -
        self.minibatch_duration_running_avg = 0.0
        self.minibatch_duration_avg_coeff = 0.05  # auto-regressive running average coefficient
        self.last_minibatch_start_datetime = datetime.datetime.now()
        self.epoch_start_datetimes = [datetime.datetime.now()]  # This value can be erased in init_with_model
        # - - - - - Tensorboard / Comet - - - - -
        self.tensorboard, self.comet = None, None
        if 'tensorboard' in self.logger_type:
            warnings.warn("Tensorboard experiment logging is not maintained anymore", category=DeprecationWarning)
            self.tensorboard = TensorboardSummaryWriter(log_dir=self.tensorboard_run_dir, flush_secs=5,
                                                        model_config=model_config, train_config=train_config)
        if 'comet' in self.logger_type:
            self.comet = CometWriter(model_config, train_config)
        # - - - - - Multi-processed plotting (plot time: approx. 20s / plotted epoch) - - - - -
        # Use multiprocessing if required by args, and if PyCharm debugger not detected
        self.use_multiprocessing = use_multiprocessing and (not (sys.gettrace() is not None))
        # Processes will be started and joined from those threads
        self.figures_threads = [None, None]  # type: List[Optional[threading.Thread]]

    @staticmethod
    def _make_dirs_if_dont_exist(dir_path):
        if not os.path.exists(dir_path):
            os.makedirs(dir_path)

    def _make_model_run_dirs(self):
        """ Creates (no check) the directories for storing config and saved models. """
        os.makedirs(self.run_dir)
        os.makedirs(self.checkpoints_dir)
        os.makedirs(self.tensorboard_run_dir)  # Always required for profiling data

    def init_with_model(self, main_model, input_tensor_size, write_graph=True):
        """ Finishes to initialize this logger given the fully-build model. This function must be called
         after all checks (configuration consistency, etc...) have been performed, because it overwrites files. """
        # Write config file on startup only - any previous config file will be erased
        # New/Saved configs compatibility must have been checked before calling this function
        if self.comet is not None:  # Log the entire config.py file to comet
            self.comet.experiment.log_code(file_name=pathlib.Path(__file__).parent.parent.joinpath('config.py'))
        # TODO check this after config refactoring
        config_dict = {'model': self.model_config.__dict__, 'train': self.train_config.__dict__}
        with open(self.run_dir.joinpath('config.json'), 'w') as f:
            json.dump(config_dict, f)
        if not self.restart_from_checkpoint:  # Graphs written at epoch 0 only
            self.write_model_summary(main_model, input_tensor_size, 'VAE')
            if write_graph and self.tensorboard is not None:
                self.tensorboard.add_graph(main_model, torch.zeros(input_tensor_size))
        self.epoch_start_datetimes = [datetime.datetime.now()]

    def log_hyper_parameters(self):
        if self.tensorboard is not None:
            raise NotImplementedError()
        if self.comet is not None:
            self.comet.log_config_hparams(self.model_config, self.train_config)

    def write_model_summary(self, model, input_tensor_size, model_name):
        if not self.restart_from_checkpoint:  # Graphs written at epoch 0 only
            description = model.get_detailed_summary()
            if self.comet is not None:
                self.comet.experiment.set_model_graph(description)
            with open(self.run_dir.joinpath('torchinfo_summary_{}.txt'.format(model_name)), 'w') as f:
                f.write(description)

    def get_previous_config_from_json(self):
        with open(self.run_dir.joinpath('config.json'), 'r') as f:
            full_config = json.load(f)
        return full_config

    def on_train_minibatch_finished(self, minibatch_idx):
        self.current_step += 1
        if self.comet is not None:
            self.comet.experiment.set_step(self.current_step)
        minibatch_end_time = datetime.datetime.now()
        delta_t = (minibatch_end_time - self.last_minibatch_start_datetime).total_seconds()
        self.minibatch_duration_running_avg *= (1.0 - self.minibatch_duration_avg_coeff)
        self.minibatch_duration_running_avg += self.minibatch_duration_avg_coeff * delta_t
        if self.verbosity >= 3:
            print("epoch {} batch {} delta t = {}ms" .format(len(self.epoch_start_datetimes)-1, minibatch_idx,
                                                             int(1000.0 * self.minibatch_duration_running_avg)))
        self.last_minibatch_start_datetime = minibatch_end_time

    def on_epoch_starts(self, epoch: int, scalars, super_metrics):
        self.current_epoch = epoch
        for _, s in super_metrics.items():
            s.on_new_epoch()
        for _, s in scalars.items():
            s.on_new_epoch()
        if self.comet is not None:
            self.comet.experiment.set_epoch(self.current_epoch)
            self.comet.experiment.set_step(self.current_step)  # To ensure step actualisation for epoch 0

    @property
    def should_plot(self):
        """ Returns whether this epoch should be plotted (costs a lot of CPU and disk space) or not. """
        return (self.current_epoch % self.train_config.plot_period == 0) \
            and (self.train_config.plot_epoch_0 or self.current_epoch > 0)

    def on_epoch_finished(self, epoch):
        if epoch != self.current_epoch:
            raise RuntimeError("Given epoch is different from epoch given to on_epoch_starts(...)")
        self.epoch_start_datetimes.append(datetime.datetime.now())
        epoch_duration = self.epoch_start_datetimes[-1] - self.epoch_start_datetimes[-2]
        avg_duration_s = np.asarray([(self.epoch_start_datetimes[i+1] - self.epoch_start_datetimes[i]).total_seconds()
                                     for i in range(len(self.epoch_start_datetimes) - 1)])
        avg_duration_s = avg_duration_s.mean()
        run_total_epochs = self.train_config.n_epochs - self.train_config.start_epoch
        remaining_datetime = avg_duration_s * (run_total_epochs - (epoch-self.train_config.start_epoch) - 1)
        remaining_datetime = datetime.timedelta(seconds=int(remaining_datetime))
        if self.verbosity >= 1:
            print("End of epoch {} ({}/{}). Duration={:.1f}s, avg={:.1f}s. Estimated remaining time: {} ({})"
                  .format(epoch, epoch-self.train_config.start_epoch+1, run_total_epochs,
                          epoch_duration.total_seconds(), avg_duration_s,
                          remaining_datetime, humanize.naturaldelta(remaining_datetime)))
        if self.comet is not None:
            self.comet.experiment.log_epoch_end(epoch_cnt=self.train_config.n_epochs)

    def save_checkpoint(self, ae_model: model.base.TrainableModel,
                        reg_model: Optional[model.base.TrainableModel] = None):
        # These keys will be re-used by model/base.py, TrainableModel::load_checkpoint(...)
        checkpoint_dict = {'epoch': self.current_epoch,
                           'ae': {'model_state_dict': ae_model.state_dict(),
                                  'optimizer_state_dict': ae_model.optimizer.state_dict(),
                                  'scheduler_state_dict': ae_model.scheduler.state_dict()}}
        if reg_model is not None:
            checkpoint_dict['reg'] = {'model_state_dict': reg_model.state_dict(),
                                      'optimizer_state_dict': reg_model.optimizer.state_dict(),
                                      'scheduler_state_dict': reg_model.scheduler.state_dict()}
        checkpoint_path = self.checkpoints_dir.joinpath('{:05d}.tar'.format(self.current_epoch))
        torch.save(checkpoint_dict, checkpoint_path)
        if self.verbosity >= 1:
            print("[RunLogger] Saved epoch {} checkpoint (models, optimizers, schedulers) to {}"
                  .format(self.current_epoch, checkpoint_path))

    def on_training_finished(self):
        # wait for all plotting threads to join
        print("[logger.py] Waiting for comet/tensorboard plotting threads to join...")
        for t in self.figures_threads:
            if t is not None:
                t.join()
        if self.tensorboard is not None:
            self.tensorboard.flush()
            self.tensorboard.close()
        if self.comet is not None:
            self.comet.experiment.end()
        if self.train_config.verbosity >= 1:
            print("[RunLogger] Training has finished")

    # - - - - - - - - - - Scalars - - - - - - - - - -

    def add_scalars(self, scalars):
        for k, s in scalars.items():
            # don't need to log everything for every train procedure (during pre-train, if no flow, etc...)
            if (self.train_config.pretrain_ae_only and (k.startswith('Controls') or k.startswith('Sched/Controls'))) \
                    or (k.startswith('LatCorr/zK') and self.model_config.latent_flow_arch is None):
                pass
            else:  # ok, we can log this
                try:
                    scalar_value = s.get()  # .get might raise except if empty/unused scalar
                except ValueError:  # unused scalars with buffer (e.g. during pretrain) will raise that exception
                    continue
                if self.tensorboard is not None:
                    self.tensorboard.add_scalar(k, scalar_value, self.current_epoch)
                if self.comet is not None:
                    self.comet.log_metric_with_context(k, scalar_value)

    # - - - - - - - - - - Non-threaded plots to comet/tensorbard - - - - - - - - - -

    def add_figure(self, name: str, fig):
        if self.tensorboard is not None:
            self.tensorboard.add_figure(name, fig, self.current_epoch)
        if self.comet is not None:
            self.comet.log_figure_with_context(name, fig)

    def plot_spectrograms(self, x_in, x_out, sample_info, dataset: AudioDataset, name='Spectrogram/Valid'):
        fig, _ = utils.figures.plot_train_spectrograms(
            x_in, x_out, sample_info, dataset, self.model_config, self.train_config)
        if self.tensorboard is not None:
            self.tensorboard.add_figure(name, fig, self.current_epoch)
        if self.comet is not None:
            self.comet.log_figure_with_context(name, fig)

    def plot_decoder_interpolation(self, generative_model, z_minibatch, sample_info, dataset: AudioDataset,
                                   output_channel_to_plot=0, name='DecoderInterp/Valid'):
        # "CUDA illegal memory access (stacktrace might be incorrect)" - seems to be fixed by torch 1.10, CUDA 11.3
        from evaluation.interp import LatentInterpolation  # local import to prevent circular import
        interpolator = LatentInterpolation(generator=generative_model, device=z_minibatch.device)
        # z start/end tensors must be be provided as 1 x D vectors
        u, z, x = interpolator.interpolate_spectrograms_from_latent(z_minibatch[0:1, :], z_minibatch[1:2, :])
        if x.shape[1] > 1:
            x = x[:, output_channel_to_plot:output_channel_to_plot+1, :, :]
        preset_UIDs = [sample_info[i, 0].item() for i in range(2)]
        preset_names = [dataset.get_name_from_preset_UID(UID) for UID in preset_UIDs]
        title = "{} '{}' ----> {} '{}'".format(preset_UIDs[0], preset_names[0], preset_UIDs[1], preset_names[1])
        fig, _ = utils.figures.plot_spectrograms_interp(u, x, z=z, title=title, plot_delta_spectrograms=True)
        if self.tensorboard is not None:
            self.tensorboard.add_figure(name, fig, self.current_epoch)
        if self.comet is not None:
            self.comet.log_figure_with_context(name, fig)

    # - - - - - Multi threaded + multiprocessing plots to comet/tensorboard - - - - -
    def plot_stats__threaded(self, super_metrics, ae_model, validation_dataset: AudioDataset):
        if self.figures_threads[0] is not None:
            if self.figures_threads[0].is_alive():
                warnings.warn("A new threaded plot request has been issued but the previous has not finished yet. "
                              "Please ignore this message if the training run is about to end. (Joining thread...)")
            self.figures_threads[0].join()
        # Data must absolutely be copied - this is multithread, not multiproc (shared data with GIL, no auto pickling)
        networks_layers_params = dict()  # If remains empty: no plot
        # Don't plot ae_model weights histograms during first epochs (will get much tinier during training)
        if self.current_epoch > 2 * self.train_config.beta_warmup_epochs:
            # FIXME re-activate, get conv weights also - returns clones of layers' parameters
            pass  # networks_layers_params['Decoder'] = ae_model.decoder.get_fc_layers_parameters()
        # Launch thread using copied data
        self.figures_threads[0] = threading.Thread(
            target=self._plot_stats_thread,
            args=(self.current_epoch, self.current_step, copy.deepcopy(super_metrics), networks_layers_params,
                  copy.deepcopy(validation_dataset))
        )
        self.figures_threads[0].name = "LoggerPlottingThread"
        self.figures_threads[0].start()

    def _plot_stats_thread(self, epoch: int, step: int, super_metrics, networks_layers_params,
                           validation_dataset: AudioDataset):
        # Asynchronous (delayed) plots: epoch and step are probably different from the current (self.) ones
        if not self.use_multiprocessing:
            figs_dict = logs.logger_mp.get_stats_figures(super_metrics, networks_layers_params)
        else:
            # 'spawn' context is slower, but solves observed deadlock
            #     - deadlock seems to be caused by serializing this instance
            #     - https://docs.python.org/3/library/multiprocessing.html 'spawn':
            #       The parent process starts a fresh python interpreter process
            ctx = multiprocessing.get_context('spawn')  # ctx used instead of multiproc
            q = ctx.Queue()
            p = ctx.Process(target=logs.logger_mp.get_stats_figs__multiproc,
                            args=(q, super_metrics, networks_layers_params))
            p.start()
            figs_dict = q.get()  # Will block until an item is available
            p.join()
            p.close()

        for fig_name, fig in figs_dict.items():
            if self.tensorboard is not None:
                self.tensorboard.add_figure(fig_name, fig, epoch, close=True)
            if self.comet is not None:
                self.comet.log_figure_with_context(fig_name, fig, step)

        # Those plots do not need to be here, but multi threading might help improve perfs a bit...
        if epoch > 0:
            try:
                for metric_name in ['RegOutValues/Train', 'RegOutValues/Valid']:
                    if self.tensorboard is not None:
                        self.tensorboard.add_vector_histograms(super_metrics[metric_name], metric_name, epoch)
                    if self.comet is not None:
                        pass  # FIXME TODO - required after pretrain only
            except AssertionError:
                pass  # RegOut metric are not filled during pretrain, and will raise errors
        # Latent metrics: TB histograms + TB embeddings (for t-SNE and UMAP visualisations)
        if self.tensorboard is not None:
            self.tensorboard.add_latent_histograms(super_metrics['LatentMetric/Train'], 'Train', epoch)
            self.tensorboard.add_latent_histograms(super_metrics['LatentMetric/Valid'], 'Valid', epoch)
        if self.comet is not None:
            self.comet.log_latent_histograms(super_metrics['LatentMetric/Train'], 'Train', epoch, step)
            self.comet.log_latent_histograms(super_metrics['LatentMetric/Valid'], 'Valid', epoch, step)
        # Validation embeddings only (Train embeddings tensor is very large, slow download and analysis)
        if self.tensorboard is not None:
            self.tensorboard.add_latent_embedding(super_metrics['LatentMetric/Valid'], 'Valid', epoch)
        if self.comet is not None:
            self.comet.log_latent_embedding(super_metrics['LatentMetric/Valid'], 'Valid', epoch, validation_dataset)
        # Network weights histograms
        for network_name, network_layers in networks_layers_params.items():  # key: e.g. 'Decoder'
            for layer_name, layer_params in network_layers.items():  # key: e.g. 'FC0'
                for param_name, param_values in layer_params.items():  # key: e.g. 'weight_abs'
                    name = '{}/{}/{}'.format(network_name, layer_name, param_name)
                    if self.tensorboard is not None:
                        # Default bins estimator: 'tensorflow'. Other estimator: 'fd' (robust for outliers)
                        self.tensorboard.add_histogram(name, param_values, epoch)
                        self.tensorboard.add_histogram('{}_no_outlier/{}/{}'.format(network_name, layer_name, param_name),
                                                       utils.stat.remove_outliers(param_values), epoch)
                    if self.comet is not None:
                        self.comet.experiment.log_histogram_3d(param_values, name, step=step, epoch=epoch)





