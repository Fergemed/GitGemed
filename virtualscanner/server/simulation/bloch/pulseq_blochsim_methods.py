# Copyright of the Board of Trustees of Columbia University in the City of New York
"""
Methods to help Bloch simulation from pulseq objects
"""

# TODO : This script is trying its best to become compatible with PyPulseq 1.3.1 right now (05/18/21)



import numpy as np
import matplotlib.pyplot as plt
import time
import virtualscanner.server.simulation.bloch.phantom as pht
import multiprocessing as mp
import virtualscanner.server.simulation.bloch.spingroup_ps as sg
import virtualscanner.server.simulation.bloch.spingroup_ps_t2star as sg2
from math import pi

GAMMA_BAR = 42.5775e6
GAMMA = 2*pi*GAMMA_BAR


def store_pulseq_commands(seq): # TODO important for compatibility with new PyPulseq
    """Converts seq file into set of commands for more efficient simulation

    Parameters
    ----------
    seq : Sequence
        Pulseq object to parse from

    Returns
    -------
    seq_info : dict
        Pulseq commands used by apply_pulseq_commands()

    """
    if hasattr(seq, 'block_events'):
        events = seq.block_events
    elif hasattr(seq, 'dict_block_events'):
        events = seq.dict_block_events

    dt_grad = seq.system.grad_raster_time
    dt_rf = seq.system.rf_raster_time
    seq_params = []

    commands = ''
    # Go through pulseq block by block and store commands
    for key in events.keys():
        event_row = events[key]
        this_blk = seq.get_block(key)

        # Case 1: Delay
        if event_row[0] != 0:
            commands += 'd'
            #seq_params.append([this_blk['delay'].delay[0]])
            seq_params.append([this_blk.delay.delay])
        # Case 2: rf pulse
        elif event_row[1] != 0:
            commands += 'p'
           # rf_time = np.array(this_blk['rf'].t[0]) - dt_rf
            rf_time = np.array(this_blk.rf.t) - dt_rf

            #df = this_blk['rf'].freq_offset
            df = this_blk.rf.freq_offset
            dph = this_blk.rf.phase_offset

            #b1 = np.multiply(np.exp(-2*pi*1j*df*rf_time),this_blk['rf'].signal/GAMMA_BAR)
            b1 = np.multiply(np.exp(1j*(-2*pi*df*rf_time + dph)), this_blk.rf.signal/GAMMA_BAR)

            rf_grad, rf_timing, rf_duration, __ = combine_gradients(blk=this_blk, timing=rf_time)
            seq_params.append([b1,rf_grad,dt_rf])

        # Case 3: ADC sampling
        elif event_row[5] != 0:
            commands += 'r'
            #adc = this_blk['adc']
            adc = this_blk.adc
            dt_adc = adc.dwell
            delay = adc.delay
            adc_phase = adc.phase_offset
            grad, timing, duration, grad_type = combine_gradients(blk=this_blk, dt=dt_adc, delay=delay)
            seq_params.append([dt_adc,int(adc.num_samples),delay,grad,timing,grad_type,adc_phase])

        # Case 4: just gradients
        elif event_row[2] != 0 or event_row[3] != 0 or event_row[4] != 0:
            commands += 'g'
            # Process gradients
            fp_grads_area = combine_gradient_areas(blk=this_blk)
            dur = find_precessing_time(blk=this_blk,dt=dt_grad)
            seq_params.append([fp_grads_area,dur])

    seq_info = {'commands':commands, 'params':seq_params,'grad_raster_time':dt_grad}
    return seq_info



def apply_pulseq_commands(isc,seq_info,store_m=False):
    """Imposes sequence commands on a single spin group

    This is the key simulation function that goes through the commands and applies each to the spin group

    Parameters
    ----------
    isc : SpinGroup
        The affected spin group
    seq_info : dict
        Commands generated by store_pulseq_commands() from a pulseq object

    """


    cmds = seq_info['commands']
    pars = seq_info['params']
    m_store = np.zeros((3,len(cmds)))

    for c in range(len(cmds)):
        cstr = cmds[c]
        cpars = pars[c]
        if cstr == 'd': # delay
            isc.delay(t=cpars[0])
        elif cstr == 'p': # rf pulse
            isc.apply_rf(pulse_shape=cpars[0],grads_shape=cpars[1],dt=cpars[2])
        elif cstr == 'r': # Readout
            if cpars[5] == 'trap':
                isc.readout_trapz(dwell=cpars[0],n=cpars[1],delay=cpars[2],grad=cpars[3],timing=cpars[4],phase=cpars[6])
            else:
                isc.readout(dwell=cpars[0],n=cpars[1],delay=cpars[2],grad=cpars[3],timing=cpars[4],phase=cpars[6])
        elif cstr == 'g': # free precessing with gradients
            isc.fpwg(grad_area=cpars[0],t=cpars[1])
        if store_m:
            #m_store[:,c] = isc.m[:,0]
            m_store[:,c] = np.squeeze(isc.get_m())

    return m_store


def apply_pulseq_old(isc,seq):
    """Deprecated function for applying a seq on a spin group and retrieving the signal
    """
    signal = []
    events = seq.block_events

    dt_grad = seq.system.grad_raster_time
    dt_rf = seq.system.rf_raster_time

    # Go through pulseq block by block and simulate
    for key in events.keys():
        event_row = events[key]
        this_blk = seq.get_block(key)

        # Case 1: Delay
        if event_row[0] != 0:
            delay = this_blk['delay'].delay[0]
            isc.delay(delay)

        # Case 2: rf pulse
        elif event_row[1] != 0:
            # Later: add ring down and dead time to be more accurate?
            rf_time = np.array(this_blk['rf'].t[0]) - dt_rf
            df = this_blk['rf'].freq_offset
            b1 = np.multiply(np.exp(-2*pi*1j*df*rf_time),this_blk['rf'].signal/GAMMA_BAR)
            rf_grad, rf_timing, rf_duration = combine_gradients(blk=this_blk, timing=rf_time)

            isc.apply_rf(b1,rf_grad,dt_rf)

        # Case 3: ADC sampling
        elif event_row[5] != 0:
            adc = this_blk['adc']
            signal_1D = []
            dt_adc = adc.dwell
            delay = adc.delay
            grad, timing, duration = combine_gradients(blk=this_blk, dt=dt_adc, delay=delay)

            isc.fpwg(grad[:,0]*delay,delay)
            v = 1
            for q in range(1,len(timing)):
                if v <= int(adc.num_samples):
                    signal_1D.append(isc.get_m_signal())
                isc.fpwg(grad[:,v]*dt_adc,dt_adc)
                v += 1
            signal.append(signal_1D)

        # Case 4: just gradients
        elif event_row[2] != 0 or event_row[3] != 0 or event_row[4] != 0:
            # Process gradients
            fp_grads_area = combine_gradient_areas(blk=this_blk)
            dur = find_precessing_time(blk=this_blk,dt=dt_grad)
            isc.fpwg(fp_grads_area,dur)
    return signal


def sim_single_spingroup_old(loc_ind,freq_offset,phantom,seq):
    """Deprecated function for applying a seq on a spin group and retrieving the signal
    """
    sgloc = phantom.get_location(loc_ind)
    isc = sg.SpinGroup(loc=sgloc, pdt1t2=phantom.get_params(loc_ind), df=freq_offset)
    signal = apply_pulseq_old(isc,seq)
    return signal


def sim_single_spingroup(loc_ind,freq_offset,phantom,seq_info,sg_type='Default',b=0,num_spins=25,
                         output_type='signal'):
    """Function for applying a seq on a spin group and retrieving the signal

    Parameters
    ----------
    loc_ind : tuple
        Index in phantom of the specific spin group
    freq_offset : float
        Off-resonance in Hertz
    phantom : Phantom
        Phantom where spin group is located
    seq_info : dict
        Commands generated by store_pulseq_commands() from a pulseq object
    sg_type : str
        Type of

    Returns
    -------
    signal : numpy.ndarray
        Complex signal consisting of all readouts stored in the SpinGroup object
    """
    sgloc = phantom.get_location(loc_ind)
    if sg_type == 'Default':
        isc = sg.SpinGroup(loc=sgloc,pdt1t2=phantom.get_params(loc_ind),df=freq_offset)
    elif sg_type == 'Solver':
        isc = sg.NumSolverSpinGroup(loc=sgloc, pdt1t2=phantom.get_params(loc_ind),df=freq_offset)
    elif sg_type == 'Diffusion':
        isc = sg.SpinGroupDiffusion(loc=sgloc, pdt1t2=phantom.get_params(loc_ind),df=freq_offset,
                                    D=phantom.get_diffusion_coeff(loc_ind),b=b)
    elif sg_type == 'T2Star':
        isc = sg2.SpinGroupT2star(loc=sgloc, pdt1t2=phantom.get_params(loc_ind), df=freq_offset,
                                  t2star=phantom.get_t2star(loc_ind),num_spins=num_spins)

    apply_pulseq_commands(isc,seq_info)

    if output_type == 'signal':
        return isc.signal
    elif output_type == 'spingroup':
        return isc


# Helpers
def combine_gradient_areas(blk): # TODO
    """Helper function that combines gradient areas in a pulseq block

    Parameters
    ----------
    blk : dict
        Pulseq block obtained from seq.get_block()

    Returns
    -------
    grad_areas : numpy.ndarray
        [Gx_area, Gy_area, Gz_area]
        Gradient areas converted into units of seconds*Tesla/meter
    """
    grad_areas = []
    for g_name in ['gx','gy','gz']:
        #if blk.__contains__(g_name):
        if hasattr(blk,g_name):
            #g = blk[g_name]
            g = blk.__getattribute__(g_name)
            g_area = g.area if g.type == 'trap' else np.trapz(y=g.waveform, x=g.t)
            grad_areas.append(g_area)
        else:
            grad_areas.append(0)
    return np.array(grad_areas)/GAMMA_BAR


def combine_gradients(blk,dt=0,timing=(),delay=0):
    """Helper function that merges multiple gradients into a format for simulation

    Interpolate x, y, and z gradients starting from time 0
    at dt intervals, for as long as the longest gradient lasts
    and combine them into a 3 x N array

    Parameters
    ----------
    blk : dict
        Pulseq block obtained from seq.get_block()
    dt : float, optional
        Raster time used in interpolating gradients, in seconds
        Default is 0 - in this case, timing is supposed to be inputted
    timing : numpy.ndarray, optional
        Time points at which gradients are interpolated, in seconds
        Default is () - in this case, dt is supposed to be inputted
    delay : float, optional
            Adds an additional time interval in seconds at the beginning of the interpolation
            Default is 0; when nonzero it is only used in ADC sampling to realize ADC delay

    Returns
    -------
    grad : numpy.ndarray
        Gradient shape in Tesla/meter
    grad_timing : numpy.ndarray
        Gradient timing in seconds
    duration: float
        Duration of input block in seconds

    Notes
    -----
    Only input one argument between dt and timing and not the other

    """
    grad_timing = []
    duration = 0
    if dt != 0:
        duration = find_precessing_time(blk,dt)
        grad_timing = np.concatenate(([0],np.arange(delay,duration+dt,dt)))
    elif len(timing) != 0:
        duration = timing[-1] - timing[0]
        grad_timing = timing

    grad = []
    g = None
    # Interpolate gradient values at desired time points
    for g_name in ['gx','gy','gz']:
       # if blk.__contains__(g_name):
        if hasattr(blk, g_name):
            #g = blk[g_name]
            g = blk.__getattribute__(g_name)
            g_time, g_shape = ([0, g.rise_time, g.rise_time + g.flat_time, g.rise_time + g.flat_time + g.fall_time],
                               [0,g.amplitude/GAMMA_BAR,g.amplitude/GAMMA_BAR,0]) if g.type == 'trap'\
                               else (g.t, g.waveform/GAMMA_BAR)
            g_time = np.array(g_time)
            grad.append(np.interp(x=grad_timing,xp=g_time,fp=g_shape))
        else:
            grad.append(np.zeros(np.shape(grad_timing)))
    if g is not None:
        grad_type = g.type
    else:
        grad_type = None
    return np.array(grad), grad_timing, duration, grad_type


def find_precessing_time(blk,dt): # TODO
    """Helper function that finds and returns longest duration among Gx, Gy, and Gz for use in SpinGroup.fpwg()

    Parameters
    ----------
    blk : dict
        Pulseq Block obtained from seq.get_block()
    dt : float
        Gradient raster time for calculating duration of only arbitrary gradients ('grad' instead of 'trap')

    Returns
    -------
    max_time : float
        Maximum gradient time, in seconds, among the three gradients Gx, Gy, and Gz

    """
    grad_times = []
    for g_name in ['gx','gy','gz']:
        #if blk.__contains__(g_name):
        if hasattr(blk, g_name):
            #g = blk[g_name]
            g = blk.__getattribute__(g_name)
            tg = (g.rise_time + g.flat_time + g.fall_time) if g.type == 'trap' else len(np.squeeze(g.t))*dt
            grad_times.append(tg)

    if len(grad_times) != 0:
        return max(grad_times)
    else:
        return 0.0


def get_dB0_map(maptype=0):
    """Returns a predefined B0 map for simulating effects of B0 inhomogeneity

    Parameters
    ----------
    maptype : int
        Index for retrieving a map.
        1 - linear map (center out)
        2 - quadratic map (center out)
        Others - uniform map

    Returns
    -------
    dB0_map : function
        This function takes location (x,y,z) as a single parameter and returns delta B0 in Tesla

    """
    if maptype == 1:
        # Linear field (~gradient)
        def dB0_map(loc):
            b0_sc = 1e-4 # TODO what's a good value?
            return b0_sc * np.sqrt(loc[0] * loc[0] + loc[1] * loc[1] + loc[2] * loc[2])
    elif maptype == 2:
        # Quadratic field
        def dB0_map(loc):
            b0_sc = 1e-3  # TODO what's a good value?
            return b0_sc * (loc[0] * loc[0] + loc[1] * loc[1] + loc[2] * loc[2])
    else:
        def dB0_map(loc):
            return 0
    return dB0_map