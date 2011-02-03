from __future__ import division; __metaclass__ = type

import itertools, random
import numpy
from wemd.types import Segment, Particle

from itertools import izip

class WEMDSystem:
    INITDIST_NAME = 0
    INITDIST_PROB = 1
    INITDIST_PCOORD = 2
    INITDIST_BIN = 3
    
    def __init__(self, sim_manager):
        self.sim_manager = sim_manager
        
        # The progress coordinate region set
        self.region_set = None
        
        # The initial distribution, a list of (name, probability, initial pcoord) tuples
        self.initial_distribution = None
        
        # Number of dimentions in progress coordinate data
        self.pcoord_ndim = 1
        
        # Length of progress coordinate data for each segment
        self.pcoord_len = 1
        
        # Data type of progress coordinate
        self.pcoord_dtype = numpy.float64
        
    def preprocess_segment(self, segment):
        '''Perform pre-processing on a given segment.  This is run by the worker immediately before propagation.'''
        pass
        
    def postprocess_segment(self, segment):
        '''Perform post-processing on a given segment.  This is run by the worker immediately after propagation.'''
        pass
    
    def segments_to_particles(self, segments):
        '''Convert segments to particles.  This is run by the master immediately before weighted ensemble.
        May be overridden to specify custom conversion (e.g. assigning a progress coordinate that absolutely
        cannot be generated by postprocessing for some odd reason)'''
        particles = []
        for segment in segments:
            particle = Particle(particle_id = segment.seg_id,
                                weight = segment.weight,
                                parent_id = None, # only set for split or merged particles; has no relation to segment parent_ids
                                parent_ids = None,
                                pcoord = segment.pcoord[-1])
            particles.append(particle)
            
    def particles_to_segments(self, particles):
        segments = []
        pcoord = numpy.empty((1,self.pcoord_ndim), self.pcoord_dtype)
        for particle in particles:
            segment = Segment(seg_id = None, # assigned by data manager)
                              status = Segment.SEG_STATUS_PREPARED,
                              p_parent_id = particle.p_parent_id,
                              parent_ids = particle.parent_ids,
                              n_parents = len(particle.parent_ids),
                              endpoint_type = Segment.SEG_ENDPOINT_TYPE_NOTSET,
                              weight = particle.weight)
            pcoord[0] = particle.pcoord
            segment.pcoord = pcoord
            segments.append(segment)
        return segments