from __future__ import division, print_function; __metaclass__ = type

import logging

log = logging.getLogger(__name__)

import numpy
from itertools import izip

import wemd
from wemdtools.aframe import AnalysisMixin
from wemdtools.aframe.trajwalker import TrajWalker

class TransitionEventAccumulator:
    index_dtype  = numpy.uintp
    count_dtype  = numpy.uint64
    weight_dtype = numpy.float64
    output_tdat_chunksize = 10000    # HDF5 chunksize for transition data (~500 KiB)
    tdat_buffersize = 1000000         # Internal buffer length (~50 MiB)
    
    def __init__(self, n_bins, output_group):
        self.n_bins = n_bins
        self.iibins = numpy.arange(n_bins)
        self.iibdisc = numpy.empty((n_bins,), numpy.bool_)
        
        self.bin_index_dtype = numpy.min_scalar_type(n_bins)
        
        self.tdat_dtype = numpy.dtype( [('block',           self.index_dtype),
                                        ('timepoint',       self.index_dtype),
                                        ('initial_bin',     self.bin_index_dtype),
                                        ('final_bin',       self.bin_index_dtype),
                                        ('initial_weight',  self.weight_dtype),
                                        ('final_weight',    self.weight_dtype),
                                        ('initial_bin_pop', self.weight_dtype),
                                        ('duration',        self.index_dtype),
                                        ('fpt',             self.index_dtype),
                                        ])
        
        
        # HDF5 group in which to store results
        self.output_group = output_group
        self.tdat_buffer = numpy.empty((self.tdat_buffersize,), dtype=self.tdat_dtype)
        self.tdat_buffer_offset = 0
        self.output_tdat_offset = 0
        self.output_tdat_ds = None
                        
        # Accumulators/counters
        self.n_trans           = None # shape (n_bins,n_bins)

        # Time points and per-timepoint data
        self.last_exit          = None # (n_bins,)
        self.last_entry         = None # (n_bins,)
        self.last_completion    = None # (n_bins,n_bins)
        self.weight_last_exit   = None # (n_bins) 
        self.bin_pops_last_exit = None # (n_bins,)       
        
        # Analysis continuation information
        self.timepoint          = None # current time index for separate calls on same trajectory
        self.last_bin           = None # last region occupied, for separate calls on same trajectory
        self.last_bin_pop       = None # total weight in self.last_region at end of last processing step
                
        self.clear()
        
    def clear(self):
        self.clear_state()
        self.n_trans         = numpy.zeros((self.n_bins,self.n_bins), self.count_dtype)
        self.tdat_buffer = numpy.empty((self.tdat_buffersize,), dtype=self.tdat_dtype)
        self.tdat_buffer_offset = 0
        self.output_tdat_offset = 0
        self.output_tdat_ds = None
        
    def clear_state(self):
        self.last_exit          = numpy.zeros((self.n_bins,), self.index_dtype)
        self.last_entry         = numpy.zeros((self.n_bins,), self.index_dtype)
        self.last_completion    = numpy.zeros((self.n_bins,self.n_bins), self.index_dtype)
        self.weight_last_exit   = numpy.zeros((self.n_bins,), self.weight_dtype)
        self.bin_pops_last_exit = numpy.zeros((self.n_bins,), self.weight_dtype)        
        self.timepoint          = 0
        self.last_bin           = None
        self.last_bin_pop       = None        

    def get_state(self):
        return {'last_entry':           self.last_entry.copy(),
                'last_exit':            self.last_exit.copy(),
                'last_completion':      self.last_completion.copy(),
                'weight_last_exit':     self.weight_last_exit.copy(),
                'bin_pops_last_exit':   self.bin_pops_last_exit.copy(),
                'timepoint':            self.timepoint,
                'last_bin':             self.last_bin,
                'last_bin_pop':         self.last_bin_pop,
                }
        
    def set_state(self, state_dict):
        self.last_entry = state_dict['last_entry']
        self.last_exit = state_dict['last_exit']
        self.last_completion = state_dict['last_completion']
        self.weight_last_exit = state_dict['weight_last_exit']
        self.bin_pops_last_exit = state_dict['bin_pops_last_exit']
        self.timepoint = state_dict['timepoint']
        self.last_bin = state_dict['last_bin']
        self.last_bin_pop = state_dict['last_bin_pop']
        
    def record_transition_data(self, tdat):
        """Update running statistics and write transition data to HDF5 (with buffering)"""        
        
        # Write out accumulated transition data
        if self.output_tdat_ds is None:        
            # Create dataset
            try:
                del self.output_group['transitions']
            except KeyError:
                pass
            
            self.output_tdat_ds = self.output_group.create_dataset('transitions', shape=(1,), 
                                                                   dtype=self.tdat_dtype, maxshape=(None,), 
                                                                   chunks=(self.output_tdat_chunksize,),
                                                                   compression='gzip')
            
        # If the amount of data to write exceeds our remaining buffer space, flush the buffer, then
        # write data directly to HDF5, otherwise just add to the buffer and wait for the last flush
        if len(tdat) + self.tdat_buffer_offset > self.tdat_buffersize:
            self.flush_transition_data()
            ub = self.output_tdat_offset + len(tdat)
            self.output_tdat_ds.resize((ub,))
            self.output_tdat_ds[self.output_tdat_offset:ub] = tdat
            self.output_tdat_offset += len(tdat)
        else:
            self.tdat_buffer[self.tdat_buffer_offset:(self.tdat_buffer_offset+len(tdat))] = tdat
            self.tdat_buffer_offset += len(tdat)
        
    def flush_transition_data(self):
        """Flush any unwritten output that may be present"""
        if self.output_tdat_ds is None:
            return
                
        # self.tdat_buffer_offset is the number of items in the buffer
        nbuf = self.tdat_buffer_offset
        if nbuf == 0: return
        ub = nbuf + self.output_tdat_offset
        if ub > self.output_tdat_ds.len():
            # Resize dataset to fit data
            self.output_tdat_ds.resize((ub,))
            
        self.output_tdat_ds[self.output_tdat_offset:ub] = self.tdat_buffer[:nbuf]
        self.output_tdat_offset += nbuf
        self.tdat_buffer_offset = 0
        
    @profile
    def start_accumulation(self, assignments, weights, bin_pops, block=0):
        self.clear_state()
        timepoints = numpy.arange(len(assignments))
        self._accumulate_transitions(timepoints, assignments, weights, bin_pops, block)
    
    @profile
    def continue_accumulation(self, assignments, weights, bin_pops, block=0):
        aug_assign = numpy.empty((len(assignments)+1,), assignments.dtype)
        aug_assign[0] = self.last_bin
        aug_assign[1:] = assignments
        
        aug_weights = numpy.empty((len(weights)+1,), self.weight_dtype)
        aug_weights[0] = 0
        aug_weights[1:] = weights
        
        aug_pops = numpy.empty((len(bin_pops)+1, len(bin_pops[0])), self.weight_dtype)
        aug_pops[0,:] = 0
        aug_pops[0, self.last_bin] = self.last_bin_pop
        aug_pops[1:] = bin_pops
        
        timepoints = numpy.arange(self.timepoint, self.timepoint+len(aug_assign))
        
        self._accumulate_transitions(timepoints, aug_assign, aug_weights, aug_pops, block)
        

    @profile    
    def _accumulate_transitions(self, timepoints, assignments, weights, bin_pops, block):
        tdat = []
        
        assignments_from_1 = assignments[1:]
        assignments_to_1   = assignments[:-1]
        
        trans_occur = assignments_from_1 != assignments_to_1
        trans_ibin  = assignments_to_1[trans_occur]
        trans_fbin  = assignments_from_1[trans_occur]
        trans_timepoints = timepoints[1:][trans_occur]
        trans_weights   = weights[1:][trans_occur] # arrival weights
        trans_ibinpops = bin_pops[:-1][trans_occur]
        
        last_exit = self.last_exit
        last_entry = self.last_entry
        last_completion = self.last_completion
        bin_pops_last_exit = self.bin_pops_last_exit
        weight_last_exit = self.weight_last_exit
        n_trans = self.n_trans
        iibdisc = self.iibdisc
        iibins = self.iibins
        tdat_maxlen = self.tdat_buffersize / 10
        for (trans_ti, weight, ibin, fbin, ibinpops) in izip(trans_timepoints, trans_weights, 
                                                             trans_ibin, trans_fbin, trans_ibinpops):
            # Record this crossing event's data
            
            bin_pops_last_exit[ibin] = ibinpops[ibin]            
            last_exit[ibin] = trans_ti
            last_entry[fbin] = trans_ti
            weight_last_exit[ibin] = weight
            
            iibdisc[:] = last_exit > 0
            iibdisc &= last_entry > last_completion[:,fbin]
            
            for iibin in iibins[iibdisc]:                
                duration = trans_ti - last_exit[iibin] + 1
                lcif = last_completion[iibin,fbin]
                if lcif > 0:
                    fpt = trans_ti - lcif
                else:
                    fpt = 0

                tdat.append((block, trans_ti, iibin, fbin, 
                             weight_last_exit[iibin], weight, bin_pops_last_exit[iibin], 
                             duration, fpt))
                last_completion[iibin,fbin] = trans_ti
                n_trans[iibin,fbin] += 1

            #last_exit[ibin] = trans_ti
            #last_entry[fbin] = trans_ti
            #last_completion[ibin,fbin] = trans_ti
            
            if len(tdat) > tdat_maxlen:
                self.record_transition_data(tdat)
        
        self.record_transition_data(tdat)
        self.timepoint = timepoints[-1]
        self.last_bin = assignments[-1]
        self.last_bin_pop = bin_pops[-1,assignments[-1]]

class TransitionAnalysisMixin(AnalysisMixin):
    def __init__(self):
        super(TransitionAnalysisMixin,self).__init__()
        self.__discard_transition_data = False
        
        self.ed_stats_filename = None
        self.flux_stats_filename = None
        self.rate_stats_filename = None
        self.suppress_headers = None
        self.print_bin_labels = None
        
        self.trans_h5gname = 'transitions'
        self.trans_h5group = None

    def __require_group(self):
        if self.trans_h5group is None:
            self.trans_h5group = self.anal_h5file.require_group(self.trans_h5gname)
        return self.trans_h5group

    def add_args(self, parser, upcall = True):
        if upcall:
            try:
                upfunc = super(TransitionAnalysisMixin,self).add_args
            except AttributeError:
                pass
            else:
                upfunc(parser)
        
        group = parser.add_argument_group('transition analysis options')
        group.add_argument('--discard-transition-data', dest='discard_transition_data', action='store_true',
                           help='''Discard any existing transition data stored in the analysis HDF5 file.''')
        group.add_argument('--dt', dest='dt', type=float, default=1.0,
                           help='Assume input data has a time spacing of DT (default: %(default)s).')

        output_options = parser.add_argument_group('transition analysis output options')        
        output_options.add_argument('--edstats', dest='ed_stats', default='edstats.txt',
                                    help='Store event duration statistics in ED_STATS (default: edstats.txt)')
        output_options.add_argument('--fluxstats', dest='flux_stats', default='fluxstats.txt',
                                    help='Store flux statistics in FLUX_STATS (default: fluxstats.txt)')
        output_options.add_argument('--ratestats', dest='rate_stats', default='ratestats.txt',
                                    help='Store rate statistics in RATE_STATS (default: ratestats.txt)')
        output_options.add_argument('--noheaders', dest='suppress_headers', action='store_true',
                                    help='Do not include headers in text output files (default: include headers)')
        output_options.add_argument('--binlabels', dest='print_bin_labels', action='store_true',
                                    help='Print bin labels in output files, if available (default: do not print bin labels)')

        
    
    def process_args(self, args, upcall = True):                
        self.__discard_transition_data = args.discard_transition_data
        
        self.ed_stats_filename = args.ed_stats
        self.flux_stats_filename = args.flux_stats
        self.rate_stats_filename = args.rate_stats
        self.suppress_headers = args.suppress_headers
        self.print_bin_labels = args.print_bin_labels
        
        if upcall:
            try:
                upfunc = super(TransitionAnalysisMixin,self).process_args
            except AttributeError:
                pass
            else:
                upfunc(args)

    def find_transitions(self):
        wemd.rc.pstatus('Finding transitions...')
        output_group = self.__require_group()
            
        self.n_segs_visited = 0
        self.n_total_segs = self.total_segs_in_range(self.first_iter,self.last_iter)
        self.accumulator = TransitionEventAccumulator(self.n_bins, output_group)        
        self.bin_assignments = self.binning_h5group['bin_assignments'][...]
        self.bin_populations = self.binning_h5group['bin_populations'][...]
        
        walker = TrajWalker(data_reader = self)
        walker.trace_trajectories(self.first_iter, self.last_iter, callable=self._segment_callback)
        self.accumulator.flush_transition_data()
        try:
            del output_group['n_trans']
        except KeyError:
            pass
        output_group['n_trans'] = self.accumulator.n_trans
        self.accumulator.clear()
        wemd.rc.pstatus()
        
        del self.assignments, self.populations
        self.assignments = self.populations = None
                
    def _segment_callback(self, segment, children, history):
        iiter = segment.n_iter - self.first_iter
        seg_id = segment.seg_id
        weights = numpy.empty((self.get_pcoord_len(segment.n_iter),), numpy.float64)
        weights[:] = segment.weight
        bin_pops = self.bin_populations[iiter, :, :]
        
        if len(history) == 0:
            # New trajectory
            self.accumulator.start_accumulation(self.bin_assignments[iiter, seg_id, :], weights, bin_pops, block=segment.n_iter)
        else:
            # Continuing trajectory
            self.accumulator.continue_accumulation(self.bin_assignments[iiter, seg_id, :], weights, bin_pops, block=segment.n_iter)
            
        self.n_segs_visited += 1
        
        if not wemd.rc.quiet_mode and (self.n_segs_visited % 1000 == 0 or self.n_segs_visited == self.n_total_segs):
            pct_visited = self.n_segs_visited / self.n_total_segs * 100
            wemd.rc.pstatus('\r  {:d} of {:d} segments ({:.1f}%) analyzed'.format(long(self.n_segs_visited), 
                                                                                  long(self.n_total_segs), 
                                                                                  float(pct_visited)), end='')
            wemd.rc.pflush()
        