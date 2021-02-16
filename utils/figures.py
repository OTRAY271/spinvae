"""
Utilities for plotting various figures (spectrograms, ...)
"""

import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
import librosa.display

import logs.metrics

from synth import dexed


def plot_spectrograms(specs_GT, specs_recons=None, presets_UIDs=None, print_info=False,
                      plot_error=False, error_magnitude=1.0, max_nb_specs=4, spec_ax_w=2.5, spec_ax_h=2.5,
                      add_colorbar=False):
    """
    Creates a figure and axes to plot some ground-truth spectrograms (1st row) and optional reconstructed
    spectrograms (2nd row)

    :returns: fig, axes

    :param specs_GT: Tensor: batch of ground-truth 1-channel spectrograms
    :param specs_recons: Tensor: batch of reconstructed spectrograms
    :param presets_UIDs: 1d-Tensor of preset UIDs corresponding to given spectrograms
    :param error_magnitude: Max error magnitude (used to set the error spectrogram colorbar limits to -mag, +mag)
    :param spec_ax_w: width (in figure units) of a single spectrogram
    """
    nb_specs = np.minimum(max_nb_specs, specs_GT.size(0))
    if add_colorbar:
        spec_ax_w *= 1.3
    if specs_recons is None:
        assert plot_error is False  # Cannot plot error without a reconstruction to be compared
        fig, axes = plt.subplots(1, nb_specs, figsize=(nb_specs*spec_ax_w, spec_ax_h))
        axes = [axes]  # Unsqueeze
        nb_rows = 1
    else:
        nb_rows = 2 if not plot_error else 3
        fig, axes = plt.subplots(nb_rows, nb_specs, figsize=(nb_specs*spec_ax_w, spec_ax_h*nb_rows))
    for row in range(nb_rows):
        for i in range(nb_specs):
            if row == 0:
                spectrogram = specs_GT[i, 0, :, :].clone().detach().cpu().numpy()
            elif row == 1:
                spectrogram = specs_recons[i, 0, :, :].clone().detach().cpu().numpy()
            else:
                spectrogram = specs_recons[i, 0, :, :].clone().detach().cpu().numpy()\
                              - specs_GT[i, 0, :, :].clone().detach().cpu().numpy()
            UID = presets_UIDs[i].item() if presets_UIDs is not None else None
            if print_info:
                if i == 0:
                    print("Dataset Spectrogram size: {}x{} = {} pixels\n"
                          "Original raw audio: {} samples (22.050kHz, 4.0s))"
                          .format(spectrogram.shape[0], spectrogram.shape[1],
                                  spectrogram.shape[0] * spectrogram.shape[1], 4 * 22050))  # TODO don't hardcode
                print("Dataset STFT Spectrogram UID={}: min={:.1f} max={:.1f} (normalized dB)"
                      .format(UID, spectrogram.min(), spectrogram.max()))
            if row == 0 and UID is not None:
                axes[row][i].set(title="{}".format(UID))
            im = librosa.display.specshow(spectrogram, shading='flat', ax=axes[row][i],
                                          cmap=('magma' if row < 2 else 'bwr'),
                                          vmin=(-error_magnitude if row == 2 else None),
                                          vmax=(error_magnitude if row == 2 else None))
            if add_colorbar:
                clb = fig.colorbar(im, ax=axes[row][i], orientation='vertical')

    fig.tight_layout()
    return fig, axes


def plot_latent_distributions_stats(latent_metric: logs.metrics.LatentMetric,
                                    plot_mu=True, plot_sigma=False, figsize=None):
    """ Uses boxplots to represent the distribution of the mu and/or sigma parameters of
    latent gaussian distributions. """
    if plot_sigma or not plot_mu:
        raise NotImplementedError("todo...")
    z_mu = latent_metric.get_z('mu')
    if figsize is None:
        figsize = (0.12 * z_mu.shape[1], 3)
    fig, ax = plt.subplots(1, 1, figsize=figsize)
    sns.boxplot(data=z_mu, ax=ax, fliersize=0.3, linewidth=0.5)
    ax.set(xlabel='z', ylabel='$q_{\phi}(z|x) : \mu$')
    for tick in ax.get_xticklabels():
        tick.set_rotation(90)
        tick.set_fontsize(8)
    fig.tight_layout()
    return fig, ax


def plot_spearman_correlation(latent_metric: logs.metrics.LatentMetric):
    """ Plots the spearman correlation matrix (full, and with zeroed diagonal)
    and returns fig, axes """
    # http://jkimmel.net/disentangling_a_latent_space/ : Uncorrelated (independent) latent variables are necessary
    # but not sufficient to ensure disentanglement...
    corr = latent_metric.get_spearman_corr()
    fig, axes = plt.subplots(1, 2, figsize=(8, 4))
    im = axes[0].matshow(corr, cmap='viridis', vmin=-1.0, vmax=1.0)
    clb = fig.colorbar(im, ax=axes[0], orientation='vertical')
    axes[0].set_xlabel('Spearman corr')
    # 0.0 on diagonal - to get a better view on variations (2nd plot)
    corr = latent_metric.get_spearman_corr_zerodiag()
    max_v = np.abs(corr).max()
    im = axes[1].matshow(corr, cmap='viridis', vmin=-max_v, vmax=max_v)
    clb = fig.colorbar(im, ax=axes[1], orientation='vertical')
    axes[1].set_xlabel('zeroed diagonal')
    for ax in axes:
        for tick in ax.get_xticklabels():
            tick.set_rotation(90)
    fig.tight_layout()
    return fig, axes


def plot_synth_preset_param(ref_preset, inferred_preset=None,
                            preset_UID=None, learnable_param_indexes=None, param_names=None,
                            show_non_learnable=True, plot_error=False,
                            synth_name=None):
    # TODO show params cardinality + figsize arg
    """ Plots reference parameters values of 1 preset, and their corresponding reconstructed values if given.

    :param ref_preset: A ground-truth preset (must be full is learnable_param_idx is None)
    :param inferred_preset: Reconstructed preset (optional)
    :param learnable_param_indexes: If not None, non-learnable params will be grey-colored.
    :param param_names: List of parameter names of a full preset (optional)
    :param show_non_learnable: If False, non-learnable parameters won't be plotted.
    :param plot_error: If True, plot the difference between the reference and inferred preset (which must be provided)
    :param synth_name: Synth name (e.g. "Dexed", ...) to add synth-specific elements (params cardinality, ...)
    """
    if not show_non_learnable:  # TODO
        raise NotImplementedError()
    if plot_error:  # TODO
        raise NotImplementedError()
    if inferred_preset is not None:
        assert len(ref_preset) == len(inferred_preset)
    fig, axes = plt.subplots(1 if not plot_error else 2, 1,
                             figsize=(0.13 * len(ref_preset), 4))  # TODO dynamic fig size
    if not isinstance(axes, np.ndarray):
        axes = [axes]  # Unsqueeze for easier looped logic
    # Params cardinality: deduced from the synth arg (str)
    if synth_name == 'Dexed':
        # Gets cardinality of *all* params (including non-learnable)
        params_cardinality = [dexed.Dexed.get_param_cardinality(i) for i in range(len(ref_preset))]
        for i, cardinality in enumerate(params_cardinality):
            if cardinality >= 2:
                y_values = np.linspace(0.0, 1.0, num=cardinality, endpoint=True)
                sns.scatterplot(x=[i for _ in range(y_values.shape[0])], y=y_values, marker='_',
                                color='grey')
    else:
        raise NotImplementedError("Synth '{}' parameters cannot be displayed".format(synth_name))
    # For easier seaborn-based plot: we use a pandas dataframe
    df = pd.DataFrame({'param_idx': range(len(ref_preset)), 'ref_preset': ref_preset})
    if learnable_param_indexes is not None:
        df['is_learnable'] = [(idx in learnable_param_indexes) for idx in range(len(ref_preset))]
    else:
        df['is_learnable'] = [True for idx in range(len(ref_preset))]
    # Scatter plot for "faders" values
    sns.scatterplot(data=df, x='param_idx', y='ref_preset', ax=axes[0],
                    hue="is_learnable",
                    palette=("blend:#BBB,#06D" if learnable_param_indexes is not None else "deep"))
    if inferred_preset is not None:
        df['inferred_preset'] = inferred_preset
        sns.scatterplot(data=df, x='param_idx', y='inferred_preset', ax=axes[0],
                        hue="is_learnable",
                        palette=("blend:#BBB,#D60" if learnable_param_indexes is not None else "husl"))
    axes[0].set_xticks(range(len(ref_preset)))
    axes[0].set_xticklabels(['{}.{}'.format(idx, ('' if param_names is None else param_names[idx]))
                             for idx in range(len(ref_preset))])
    axes[0].set(xlabel='', ylabel='Param. value', xlim=[0-0.5, len(ref_preset)-0.5])
    axes[0].get_legend().remove()
    if preset_UID is not None:
        axes[0].set_title("Preset UID={}".format(preset_UID))
    plt.vlines(x=np.arange(len(ref_preset) + 1) - 0.5, ymin=0.0, ymax=1.0, colors='k', linewidth=1.0)
    # vertical "faders" separator lines
    for tick in axes[0].get_xticklabels():
        tick.set_rotation(90)
    fig.tight_layout()
    return fig, axes
