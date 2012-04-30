# -*- coding: utf-8 -*-
#_____________________________________________________________________________
#
# Copyright (C) 2011 by Philipp Meier, Felix Franke and
# Berlin Institute of Technology
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
# ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#_____________________________________________________________________________
#
# Affiliation:
#   Bernstein Center for Computational Neuroscience (BCCN) Berlin
#     and
#   Neural Information Processing Group
#   School for Electrical Engineering and Computer Science
#   Berlin Institute of Technology
#   FR 2-1, Franklinstrasse 28/29, 10587 Berlin, Germany
#   Tel: +49-30-314 26756
#_____________________________________________________________________________
#
# Acknowledgements:
#   This work was supported by Deutsche Forschungs Gemeinschaft (DFG) with
#   grant GRK 1589/1
#     and
#   Bundesministerium für Bildung und Forschung (BMBF) with grants 01GQ0743
#   and 01GQ0410.
#_____________________________________________________________________________
#

"""implementation of spike sorting with optimal linear filters

See:
[1] F. Franke, M. Natora, C. Boucsein, M. Munk, and K. Obermayer. An online
spike detection and spike classification algorithm capable of instantaneous
resolution of overlapping spikes. Journal of Computational Neuroscience, 2009
[2] F. Franke, ... , K. Obermayer, 2012,
The revolutionary BOTM Paper
"""

__docformat__ = 'restructuredtext'
__all__ = ['ABOTMNode', 'BOTMNode', 'BayesOptimalTemplateMatchingNode',
           'FilterBankSortingNode']

##---IMPORTS

import scipy as sp
from scipy import linalg as sp_la
from spikeplot import COLOURS, mcdata, plt
import warnings
from .base_nodes import Node
from .filter_bank import FilterBankError, FilterBankNode
from .spike_detector import SDMteoNode, ThresholdDetectorNode
from ..common import (
    overlaps, epochs_from_spiketrain_set, shifted_matrix_sub, mcvec_to_conc,
    epochs_from_binvec, merge_epochs, matrix_argmax, dict_list_to_ndarray,
    get_cut, get_aligned_spikes, GdfFile, MxRingBuffer)

##---CONSTANTS

MTEO_DET = SDMteoNode
MTEO_PARAMS = tuple(), {'kvalues':[6, 9, 13, 18], 'threshold_factor':3.0, }

##---CLASSES

class FilterBankSortingNode(Node):
    """abstract class that handles filter instances and their outputs

    This class provides a pipeline structure to implement spike sorting
    algorithms that operate on a filter bank. The implementation is done by
    implementing `self._pre_filter`, `self._post_filter`, `self._sort_chunk`
    and `self._post_sort` methods with meaning full processing. After the
    filter steps the filter output of the filters constituting the filter
    bank is present and can be processed on. Input data can be partitioned
    into chunks of smaller size.
    """

    def __init__(self, **kwargs):
        """
        :type templates: ndarray
        :keyword templates: templates to initialise the filter stack.
            [ntemps][tf][nc] a tensor of templates
            Required
        :type ce: TimeSeriesCovE
        :keyword ce: covariance estimator instance, if None a new instance
            will be created and initialised with the identity matrix
            corresponding to the template size.
            Required
        :type chan_set: tuple
        :keyword chan_set: tuple of int designating the subset of channels
            this filter bank operates on.
            Default=tuple(range(nc))
        :type filter_cls: FilterNode
        :keyword filter_cls: the class of filter node to use for the filter
            bank, this must be a subclass of 'FilterNode'.
            Default=MatchedFilterNode
        :type rb_cap: int
        :keyword rb_cap: capacity of the ringbuffer that stored observations
            for the filters to calculate the mean template.
            Default=350
        :type chunk_size: int
        :keyword chunk_size: if input data will be longer than chunk_size, the
            input will be processed chunk per chunk to overcome memory sinks
            Default=100000
        :type debug: bool
        :keyword debug: if True, store intermediate results and generate
            verbose output
            Default=False
        :type dtype: dtype resolvable
        :keyword dtype: anything that resolves into a scipy dtype, like a
            string or number type
            Default=None
        """

        # kwargs
        templates = kwargs.pop('templates', None)
        tf = kwargs.get('tf', None)
        if tf is None and templates is None:
            raise FilterBankError('\'templates\' or \'tf\' are required!')
        if tf is None:
            if templates.ndim != 3:
                raise FilterBankError(
                    'templates have to be provided in a tensor of shape '
                    '[ntemps][tf][nc]!')
            kwargs['tf'] = templates.shape[1]
        chunk_size = kwargs.pop('chunk_size', 100000)
        dtype = kwargs.pop('dtype', sp.float32)
        # everything not popped goes to FilterBankNode

        # build filter bank
        bank = FilterBankNode(dtype=dtype, **kwargs)
        for key in ['ce', 'chan_set', 'filter_cls', 'rb_cap', 'tf', 'debug']:
            kwargs.pop(key, None)

        # everything not popped goes to super

        # super
        super(FilterBankSortingNode, self).__init__(dtype=dtype, **kwargs)

        # members
        self._bank = bank
        self._fout = None
        self._data = None
        self._chunk = None
        self._chunk_offset = 0
        self._chunk_size = int(chunk_size)
        self.debug = bool(self._bank.debug)
        self.rval = {}

        # create filters for templates
        for temp in templates:
            self.create_filter(temp)

    ## properties

    def get_tf(self):
        return self._bank.tf

    tf = property(get_tf)

    def get_nc(self):
        return self._bank.nc

    nc = property(get_nc)

    def get_chan_set(self):
        return self._bank.cs

    cs = property(get_chan_set)

    def get_ce(self):
        return self._bank.ce

    def set_ce(self, value):
        self._bank.ce = value

    ce = property(get_ce, set_ce)

    def get_filter_idx(self):
        return self._bank._idx_active_set

    filter_idx = property(get_filter_idx)

    def get_nfilter(self):
        return len(self.filter_idx)

    nfilter = property(get_nfilter)

    def get_template_set(self, **kwargs):
        return self._bank.get_template_set(**kwargs)

    template_set = property(get_template_set)

    def get_filter_set(self, **kwargs):
        return self._bank.get_filter_set(**kwargs)

    filter_set = property(get_filter_set)

    ## filter bank interface

    def reset_history(self):
        """sets the history to all zeros for all filters"""

        self._bank.reset_history()

    def create_filter(self, xi, check=True):
        """adds a new filter to the filter bank"""

        return self._bank.create_filter(xi, check=check)

    ## mpd.Node interface

    def is_invertible(self):
        return False

    def is_trainable(self):
        return False

    ## SortingNode interface

    def _execute(self, x):
        # inits
        self._data = x[:, self._bank.cs]
        dlen = self._data.shape[0]
        self.rval.clear()
        for i in self.filter_idx:
            self.rval[i] = []
        curr_chunk = 0
        has_next_chunk = True

        # sort per chunk
        while has_next_chunk:
            # get chunk limits
            #c_start = curr_chunk * self._chunk_size
            self._chunk_offset = curr_chunk * self._chunk_size
            clen = min(dlen, (curr_chunk + 1) * self._chunk_size)
            clen -= self._chunk_offset

            # generate data chunk and process
            self._chunk = self._data[
                          self._chunk_offset:self._chunk_offset + clen]
            self._fout = sp.empty((clen, self.nfilter))

            # filtering
            self._pre_filter()
            self._fout = self._bank(self._chunk)
            self._post_filter()

            # sorting
            self._pre_sort()
            self._sort_chunk()
            self._post_sort()

            # iteration
            curr_chunk += 1
            if self._chunk_offset + clen >= dlen:
                has_next_chunk = False
        self._combine_results()

        # return input data
        return x

    ## FilterBankSorting interface

    def _pre_filter(self):
        pass

    def _post_filter(self):
        pass

    def _pre_sort(self):
        pass

    def _post_sort(self):
        pass

    def _sort_chunk(self):
        pass

    def _combine_results(self):
        self.rval = dict_list_to_ndarray(self.rval)
        correct = int(self.tf / 2)
        for k in self.rval:
            self.rval[k] -= correct

    ## output methods

    def plot_xvft(self, ph=None, show=False):
        """plot the Xi vs F Tensor of the filter bank"""

        return self._bank.plot_xvft(ph=ph, show=show)

    def plot_template_set(self, ph=None, show=False):
        """plot the template set in a waveform plot"""

        return self._bank.plot_template_set(ph=ph, show=show)

    def plot_sorting(self, ph=None, show=False, debug=False):
        """plot the sorting of the last data chunk"""

        # create events
        ev = {}
        if self.rval is not None:
            temps = self.template_set
            for i, k in enumerate(self.filter_idx):
                if self.rval[k].any():
                    ev[k] = (temps[i], self.rval[k])

        # create colours
        cols = COLOURS[:self.nfilter]

        # calc discriminants for single units
        other = None
        if self.nfilter > 0:
            other = self._bank(self._data)
            other += self._lpr_s
            other -= [.5 * self._bank.get_xcorrs_at(i)
                      for i in xrange(self.nfilter)]

        # plot mcdata
        return mcdata(self._data, other=other, events=ev,
                      plot_handle=ph, colours=cols, show=show)

    def sorting2gdf(self, fname):
        """yield the gdf representing the current sorting"""

        GdfFile.write_gdf(fname, self.rval)


class BayesOptimalTemplateMatchingNode(FilterBankSortingNode):
    """FilterBanksSortingNode derivative for the BOTM algorithm

    Can use two implementations of the Bayes Optimal Template-Matching (BOTM)
    algorithm as presented in [2]. First implementation uses explicitly
    constructed overlap channels for the extend of the complete input
    signal, the other implementation uses subtractive interference
    cancellation (SIC) on epochs of the signal, where the template
    discriminants are greater the the noise discriminant.
    """

    ## constructor

    def __init__(self, **kwargs):
        """
        :type ovlp_taus: list
        :keyword ovlp_taus: None or list of tau values. If list of tau
            values is given, discriminant-functions for all pair-wise
            template overlap cases with the given tau values will be created
            and evaluated. If None a greedy subtractive interference
            cancellation (SIC) approach will be used.
            Default=None
        :type spk_pr: float
        :keyword spk_pr: spike prior value
            Default=1e-6
        :type noi_pr: float
        :keyword noi_pr: noise prior value
            Default=1e0
        """

        #kwargs
        ovlp_taus = kwargs.pop('ovlp_taus', None)
        noi_pr = kwargs.pop('noi_pr', 1e0)
        spk_pr = kwargs.pop('spk_pr', 1e-6)

        # super
        super(BayesOptimalTemplateMatchingNode, self).__init__(**kwargs)

        # members
        self._ovlp_taus = ovlp_taus
        if self._ovlp_taus is not None:
            self._ovlp_taus = list(self._ovlp_taus)
            if self.debug is True:
                print 'using overlap channels'
        else:
            if self.debug is True:
                print 'using subtractive interference cancelation'
        self._disc = None
        self._pr_n = None
        self._lpr_n = None
        self._pr_s = None
        self._lpr_s = None
        self._oc_idx = None
        self._debug_res = None
        self.noise_prior = noi_pr
        self.spike_prior = spk_pr

    ## properties

    def get_noise_prior(self):
        return self._pr_n

    def set_noise_prior(self, value):
        if value <= 0.0:
            raise ValueError('noise prior <= 0.0')
        self._pr_n = float(value)
        self._lpr_n = sp.log(self._pr_n)

    noise_prior = property(get_noise_prior, set_noise_prior)

    def get_spike_prior(self):
        return self._pr_s

    def set_spike_prior(self, value):
        if value <= 0.0:
            raise ValueError('spike prior <= 0.0')
        self._pr_s = float(value)
        self._lpr_s = sp.log(self._pr_s)

    spike_prior = property(get_spike_prior, set_spike_prior)

    ## filter bank implementation

    def _post_filter(self):
        """build discriminant functions, prepare for sorting"""

        # tune filter outputs to prob. model
        ns = self._fout.shape[0]
        nf = self.nfilter
        if self._ovlp_taus is not None:
            nf += nf * (nf - 1) * 0.5 * len(self._ovlp_taus)
        self._disc = sp.empty((ns, nf), dtype=self.dtype)
        self._disc[:] = sp.nan
        for i in xrange(self.nfilter):
            self._disc[:, i] = (self._fout[:, i] + self._lpr_s -
                                .5 * self._bank.get_xcorrs_at(i))

        # build overlap channels from filter outputs for overlap channels
        if self._ovlp_taus is not None:
            self._oc_idx = {}
            oc_idx = self.nfilter
            for f0 in xrange(self.nfilter):
                for f1 in xrange(f0 + 1, self.nfilter):
                    for tau in self._ovlp_taus:
                        self._oc_idx[oc_idx] = (f0, f1, tau)
                        f0_lim = [max(0, 0 - tau), min(ns, ns - tau)]
                        f1_lim = [max(0, 0 + tau), min(ns, ns + tau)]
                        self._disc[f0_lim[0]:f0_lim[1], oc_idx] = (
                            self._disc[f0_lim[0]:f0_lim[1], f0] +
                            self._disc[f1_lim[0]:f1_lim[1], f1] -
                            self._bank.get_xcorrs_at(f0, f1, tau))
                        oc_idx += 1

    def _sort_chunk(self):
        """sort this chunk on the calculated discriminant functions

        method: "och"
            Examples for overlap samples
                  tau=-2     tau=-1      tau=0      tau=1      tau=2
            f1:  |-----|    |-----|    |-----|    |-----|    |-----|
            f2:    |-----|   |-----|   |-----|   |-----|   |-----|
            res:    +++       ++++      +++++      ++++       +++
        method: "sic"
            TODO:
        """

        # inits
        spk_ep = epochs_from_binvec(
            sp.nanmax(self._disc, axis=1) > self._lpr_n)
        if spk_ep.size == 0:
            return
        min_dist = self.tf / 2
        min_size = int(self.tf * 1.2)
        for i in xrange(spk_ep.shape[0]):
            s = spk_ep[i, 1] - spk_ep[i, 0]
            if s < min_size:
                l, r = get_cut(min_size - s)
                spk_ep[i, 0] -= l
                spk_ep[i, 1] += r

        # check epochs
        spk_ep = merge_epochs(spk_ep, min_dist=min_dist)
        n_ep = spk_ep.shape[0]

        for i in xrange(n_ep):
            #
            # method: overlap channels
            #
            if self._ovlp_taus is not None:
                # get event time and channel
                ep_t, ep_c = matrix_argmax(
                    self._disc[spk_ep[i, 0]:spk_ep[i, 1]])
                ep_t += spk_ep[i, 0]

                # lets fill in the results
                if ep_c < self.nfilter:
                    # was single unit
                    fid = self._bank.get_fid_for(ep_c)
                    self.rval[fid].append(ep_t + offset)
                else:
                    # was overlap
                    my_oc_idx = self._oc_idx[ep_c]
                    fid0 = self._bank.get_fid_for(my_oc_idx[0])
                    self.rval[fid0].append(ep_t + offset)
                    fid1 = self._bank.get_fid_for(my_oc_idx[1])
                    self.rval[fid1].append(
                        ep_t + my_oc_idx[2] + offset)

            #
            # method: subtractive interference cancelation
            #
            else:
                ep_fout = self._fout[spk_ep[i, 0]:spk_ep[i, 1], :]
                ep_fout_norm = sp_la.norm(ep_fout)
                ep_disc = self._disc[spk_ep[i, 0]:spk_ep[i, 1], :].copy()

                niter = 0
                while sp.nanmax(ep_disc) > self._lpr_n:
                    # fail on spike overflow
                    niter += 1
                    if niter > self.nfilter:
                        warnings.warn(
                            'more spikes than filters found! '
                            'epoch: [%d:%d] %d' % (
                                spk_ep[i][0] + self._chunk_offset,
                                spk_ep[i][1] + self._chunk_offset,
                                niter))
                        if niter > 2 * self.nfilter:
                            break

                    # find spike classes
                    ep_t = sp.nanargmax(sp.nanmax(ep_disc, axis=1))
                    ep_c = sp.nanargmax(ep_disc[ep_t])

                    # build subtrahend
                    sub = shifted_matrix_sub(
                        sp.zeros_like(ep_disc),
                        self._bank.xcorrs[ep_c, :, :].T,
                        ep_t - self.tf + 1)

                    # apply subtrahend
                    if ep_fout_norm > sp_la.norm(ep_fout + sub):
                        if self.debug is True:
                            x_range = sp.arange(
                                spk_ep[i, 0] + self._chunk_offset,
                                spk_ep[i, 1] + self._chunk_offset)
                            f = plt.figure()
                            f.suptitle('spike epoch [%d:%d] #%d' %
                                       (spk_ep[i, 0] + self._chunk_offset,
                                        spk_ep[i, 1] + self._chunk_offset,
                                        niter))
                            ax1 = f.add_subplot(211)
                            ax1.plot(x_range, sp.zeros_like(x_range), 'k--')
                            ax1.plot(x_range, ep_disc)
                            ax1.axvline(x_range[ep_t])
                            ax2 = f.add_subplot(212, sharex=ax1, sharey=ax1)
                            ax2.plot(x_range, sub)
                            ax2.axvline(x_range[ep_t])
                        ep_disc += sub + self._lpr_s
                        if self.debug is True:
                            ax1.plot(x_range, ep_disc, ls=':', lw=2)
                        fid = self._bank.get_fid_for(ep_c)
                        self.rval[fid].append(
                            spk_ep[i, 0] + ep_t + self._chunk_offset)
                    else:
                        break
                del ep_fout, ep_disc, sub

    ## BOTM implementation

    def eval_prob(self, data):
        """evaluate the probability of the data at a sample"""

        data = mcvec_to_conc(data)
        resi = data - self.get_template_set(mc=False)
        maha = sp.zeros(len(resi))
        for i in xrange(len(resi)):
            maha[i] = sp.dot(data[i],
                             sp.dot(self.ce.get_icmx(tf=self.tf), data[i]))
        lp = -.5 * maha + self._lpr_s
        return lp / lp.sum()

# for legacy compatibility
BOTMNode = BayesOptimalTemplateMatchingNode

class ABOTMNode(BayesOptimalTemplateMatchingNode):
    """Adaptive BOTM Node

    tries to match parallel detection with sorting to find new units.
    """

    def __init__(self, **kwargs):
        """
        :type learn_templates: int
        :keyword learn_templates: if non-negative integer, adapt the filters
            with the found events aligned at that sample. If negative,
            calculate the alignment samples as int(.25*self.tf)
            Default=-1
        :type learn_noise: str or None
        :keyword learn_noise: if not None, adapt the noise covariance matrix
            with from the noise epochs. This has to be either 'sort' to
            learn from the non overlapping sorting events,
            or 'det' to lean from the detection. Else, do not learn the noise.
            Default='sort'
        :type det_cls: ThresholdDetectorNode
        :keyword det_cls: the class of detector node to use for the spike
            detection running in parallel to the sorting,
            this must be a subclass of 'ThresholdDetectorNode'.
            Default=MTEO_DET
        :type det_limit: int
        :keyword det_limit: capacity of the ringbuffer to hold the detection
            spikes.
            Default=2000
        :type det_params: dict
        :keyword det_params: parameters for the spike detector that will be
            run in parallel on the data.
            Default=MTEO_PARAMS
        """

        # kwargs
        learn_templates = kwargs.pop('learn_templates', -1)
        learn_noise = kwargs.pop('learn_noise', 'sort')
        det_cls = kwargs.pop('det_cls', MTEO_DET)
        det_limit = kwargs.pop('det_limit', 2000)
        det_params = kwargs.pop('det_params', MTEO_PARAMS)

        # check det_cls
        if not issubclass(det_cls, ThresholdDetectorNode):
            raise TypeError(
                '\'det_cls\' of type ThresholdDetectorNode is required!')
        if learn_noise is not None:
            if learn_noise not in ['det', 'sort']:
                learn_noise = None

        # super
        super(ABOTMNode, self).__init__(**kwargs)

        if learn_templates < 0:
            learn_templates = int(0.25 * self.tf)

        # members
        self._det = None
        self._det_cls = det_cls
        self._det_params = det_params
        self._det_limit = int(det_limit)
        self._det_buf = None
        self._det_unexplained_by_fb = None
        self._learn_noise = learn_noise
        self._learn_templates = learn_templates

    ## properties

    def get_det(self):
        if self._det is None:
            self._det = self._det_cls(*self._det_params[0],
                                      **self._det_params[1])
            self._det_buf = MxRingBuffer(capacity=self._det_limit,
                                         dimension=(self.tf, self.nc),
                                         dtype=self.dtype)
        return self._det

    det = property(get_det)

    ## FilterBanksortingNode interface

    def _pre_filter(self):
        pass

    def _post_sort(self):
        self.det(self._chunk)
        det_spks = self.det.get_extracted_events(
            mc=False, align_kind='min', align_at=self._learn_templates)

    def _execute(self, x):
        # call super to get sorting
        rval = super(ABOTMNode, self)._execute(x)
        # learn new templates from detection
        self._adapt_det_spks()
        # learn slow template changes
        self._adapt_templates()
        # learn slow noise statistic changes
        self._adapt_noise()
        return rval

    ## ABOTM interface

    def _adapt_det_spks(self):
        pass

    def _adapt_noise(self):
        if self._learn_noise:
            nep = None
            if self._learn_noise == 'sort':
                nep = epochs_from_spiketrain_set(
                    self.rval, cut=self.tf, end=self._data.shape[0])['noise']
            elif self._learn_noise == 'det':
                nep = self.det.get_epochs(merge=True, invert=True)
            self.ce.update(self._data, epochs=nep)

    def _adapt_templates(self):
        """adapt templates/filters using non overlapping spikes"""

        # checks and inits
        if self._data is None or self.rval is None:
            return
        ovlp_info = overlaps(self.rval, self.tf)[0]
        cut = get_cut(self.tf)

        # adapt filters with found waveforms
        for u in self.rval:
            st = self.rval[u][ovlp_info[u] == False]
            if len(st) == 0:
                continue
            spks_u = get_aligned_spikes(
                self._data, st, cut, self._learn_templates, mc=True,
                kind='min')[0]
            if spks_u.size == 0:
                continue
            self._bank.bank[u].extend_xi_buf(spks_u)

##---MAIN

if __name__ == '__main__':
    pass
