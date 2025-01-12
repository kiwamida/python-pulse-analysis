import glob
import numpy as np
import re
from copy import copy
import pickle
import matplotlib.pyplot as plt
from scipy.signal import find_peaks, welch, resample, windows
from scipy.fft import fft
from scipy.stats import gaussian_kde
from scipy.optimize import curve_fit
from scipy.interpolate import interp1d


def get_bin_files(dir_path, kid_nr, p_read):
    txt = 'KID' + str(kid_nr) + '_' + str(p_read) + 'dBm__TDvis'
    info_path = dir_path + '/' + txt + '0' + '*_info.dat'
    bin_path = dir_path + '/' + txt + '*.bin'
    list_bin_files = glob.glob(bin_path)
    info_file = glob.glob(info_path)
    if not list_bin_files:
        raise Exception('Please correct folder path as no files were obtained using path:\n%s' % (bin_path))
    list_bin_files = sorted(list_bin_files, key=lambda s: int(re.findall(r'\d+', s)[-2]))
    return list_bin_files, info_file


def get_info(file_path):
    with open(file_path) as f:
        lines = f.readlines()
    f0 = float(re.findall("\d+\.\d+", lines[2])[0]) 
    Qs = re.findall("\d+\.\d+", lines[3])
    Qs = [float(Q) for Q in Qs]
    [Q, Qc, Qi, S21_min] = Qs
    fs = 1 / float(re.findall("\d+\.\d+", lines[4])[0])
    T = float(re.findall("\d+\.\d+", lines[7])[0])
    info = {'f0' : f0, 'Q' : Q, 'Qc' : Qc, 'Qi' : Qi, 'S21_min' : S21_min, 'fs' : fs, 'T' : T}
    return info


def bin2mat(file_path):
    data = np.fromfile(file_path, dtype='>f8', count=-1)
    data = data.reshape((-1, 2))

    I = data[:, 0]
    Q = data[:, 1]

    # From I and Q data to Radius/Magnitude and Phase
    r = np.sqrt(I**2 + Q**2)
    R = r/np.mean(r) # Normalize radius to 1


    P = np.arctan2(Q, I) 
    P = np.pi - P % (2 * np.pi) # Convert phase to be taken from the negative I axis
    return R, P


def plot_bin(file_path):
    response = bin2mat(file_path)[1]
    time = np.arange(len(response)) * 20e-6
    info = get_info(file_path[:-4]+'_info.dat')
    print(info)
    fig, ax = plt.subplots(figsize=(10, 4), constrained_layout=True)
    ax.plot(time, response, lw=.2)
    ax.set_xlabel('t [s]')
    ax.set_ylabel('$\\theta$ [rad]')
    line_len = len(file_path) // 2
    ax.set_title(file_path[:line_len] + '\n' + file_path[line_len:])
    ax.set_xlim(time[0], time[-1])


def concat_vis(file_list):
    amp = []
    phase = []
    for file in file_list:
        r, p = bin2mat(file)
        amp.append(r)
        phase.append(p)
    amp = np.array(amp).flatten()
    phase = np.array(phase).flatten()
    return amp, phase


def smith_coord(P, R):
    '''
    This function returns the phase and amplitude reponse in the Smith chart coordinate systam.
    '''
    # Normalised I and Q
    I_r = -np.cos(P) * R
    Q_r = np.sin(P) * R

    # SMith chart coordinate system
    G = I_r + 1j * Q_r
    z = (1 + G) / (1 - G)

    R_smith = np.real(z)
    X_smith = np.imag(z)
    R_smith -= np.mean(R_smith)
    X_smith -= np.mean(X_smith)
    return R_smith, X_smith


def coord_transformation(response, coord, phase, amp, dark_phase=[], dark_amp=[]):
    overshoot = -0.5 * np.pi
    
    if np.any(phase <= overshoot):
        print('WARNING: Phase response found to be larger than pi*rad')

    dark_too = True
    if len(dark_phase) == 0:
        dark_too = False

    phase[phase <= overshoot] += 2 * np.pi
    if dark_too:
        dark_phase[dark_phase <= overshoot] += 2 * np.pi

    if coord == 'smith':
        R, X = smith_coord(phase, amp)
        if dark_too:
            R_dark, X_dark = smith_coord(dark_phase, dark_amp)
        if response == 'R':
            signal = R
            if dark_too:
                dark_signal = R_dark
        elif response == 'X':
            signal = X
            if dark_too:
                dark_signal = X_dark
        else:
            raise Exception('Please input a proper response type ("R" or "X")')
    elif coord == 'circle':
        if response == 'phase':
            signal = phase - np.mean(phase)
            if dark_too:
                dark_signal = dark_phase - np.mean(dark_phase)
        elif response == 'amp':
            signal = (1 - amp) - np.mean(1 - amp)
            if dark_too:
                dark_signal = (1 - dark_amp) - np.mean(1 - dark_amp)
        else:
            raise Exception('Please input a proper response type ("amp" or "phase")')
    else:
        raise Exception('Please input a proper coordinate system ("smith" or "circle")')   
    
    if dark_too:
        return signal, dark_signal
    else:
        return signal


def supersample(signal, num, type='resample', axis=0):
    if type == 'interp1d':
        l = len(signal)
        x = np.arange(l)  
        x_ss = np.linspace(0, l-1, num)
        return interp1d(x, signal, axis=axis)(x_ss)
    elif type == 'resample':
        return resample(signal, num, axis=axis)
    else:
        raise Exception('Please input correct supersample type: "interp1d" or "resample"')


def peak_model(signal, mph, mpp, pw, sw, window, ssf, buffer, filter_std, rise_offset, plot_pulse=False, every=100, below=False):
    '''
    This function finds, filters and aligns the pulses in a timestream data
    '''
    # Smooth timestream data for peak finding
    if sw:    
        kernel = get_window(window, pw, sw)
        smoothed_signal = np.convolve(signal, kernel, mode='valid')
    else:
        smoothed_signal = signal

    # Find peaks in data
    locs_smoothed, props_smoothed = find_peaks(smoothed_signal, height=mph, prominence=mpp)
    pks_smoothed = props_smoothed['peak_heights']
    nr_pulses = len(locs_smoothed)
    det_locs = copy(locs_smoothed)
    if nr_pulses == 0:
        pulses_aligned = []
        H = []
        sel_locs = []
        filtered_locs = []
        pks_smoothed = []
    else:
        # Assign buffer for pulse alignment. The buffer is applied on both sides of the pulsewindow: buffer + pw + buffer
        buffer = int(buffer * pw)
        buffer_len = int(2 * buffer + pw)
        align_shift = int(rise_offset * pw)  # shift introduced before smoothed_loc, such that there is rise_offset*pw before pulse rise

        # Filter peak that is too close to the end of data array (too close too start is already filtered in previous step)
        length_signal = len(signal)
        if locs_smoothed[-1] + pw + buffer > length_signal:
            locs_smoothed = locs_smoothed[:-1]
            pks_smoothed = pks_smoothed[:-1]

        # Filter the peaks that are too close to one another
        diff_locs = copy(locs_smoothed)  # make shallow copy of locs
        diff_locs[1:] -= locs_smoothed[:-1]  # compute the spaces in between the peaks
        filter_left = diff_locs > buffer_len  # find all peaks that are too close to the previous peak
        filter_right = np.hstack((filter_left[1:], True))  # find the peaks that are too close to the following peak, which is just a simple shift of the previous 
        filter = filter_left & filter_right  # find all peaks that are far enough from both the previous as the following peak
        locs_smoothed = locs_smoothed[filter] # filter peaks where the space in between is less than pulsewindow + 2*buffer
        pks_smoothed = pks_smoothed[filter]
        nr_far_enough = filter.sum()
        perc_too_close = round(100 * (1 - nr_far_enough / nr_pulses))

        # Cut pulses from timestream and align on smoothed peak
        sel_locs = copy(locs_smoothed)
        pulses_aligned = []
        idx_halfmax = []
        H = []
        filter_diff = np.ones(len(sel_locs), dtype=bool)
        plot_count = 0
        for i in range(len(locs_smoothed)):
            loc = locs_smoothed[i]
            left = int(loc - align_shift - buffer)
            right = int(loc + (pw - align_shift) + buffer)
            pulse = signal[left:right]    # first take a cut with buffer on both sides based on loc from smoothed data
            smoothed_pulse = smoothed_signal[left:right]
            
            # Correct for drift by substracting the mean of the signal in the buffer before and after the pulse from the pulse itself
            offset = np.mean(np.hstack((pulse[:buffer], pulse[-buffer:])))
            pulse -= offset
            smoothed_pulse -= offset

            # Supersample the peak
            if ssf and ssf > 1:
                pulse = supersample(pulse, buffer_len * ssf)
                smoothed_pulse = supersample(smoothed_pulse, buffer_len * ssf)
            else: 
                ssf = 1
            
            smoothed_loc = (buffer+align_shift) * ssf   # this is a guess of the smoothed peak based on the loc from smoothed data. The true smoothed peak and peak height still have to be determined   
            smoothed_peak = pks_smoothed[i]
            len_pulse = int(buffer_len * ssf)
            unsmoothed_height = pulse[smoothed_loc]

            if unsmoothed_height <= smoothed_peak:
                min_height = smoothed_peak
            else:
                min_height = unsmoothed_height

            # Find non-smoothed peak closest to smoothed peak
            locs_right, props_right = find_peaks(pulse[smoothed_loc:], height=min_height, prominence=0) # Find peaks to the right of smoothed peak with at minimal height the value at the smoothed peak
            if len(locs_right) != 0:
                idx_max_right = smoothed_loc + locs_right[0]
                idx_max = idx_max_right
                full_max = props_right['peak_heights'][0]
            else:
                locs_left, props_left = find_peaks(pulse[:smoothed_loc], height=min_height, prominence=0) # Find peaks to the right of smoothed peak with at minimal height the value at the smoothed peak
                if len(locs_left) != 0:
                    idx_max_left = locs_left[-1]
                    idx_max = idx_max_left
                    full_max = props_left['peak_heights'][0]   
                else:
                    filter_diff[i] = False
                    continue
            sel_locs[i] = left + idx_max

            # Align pulses on rising edge   
            half_max = full_max / 2
            rising_edge = idx_max - np.argmax(pulse[-(len_pulse - idx_max)::-1] < half_max) # Find rising edge as the first value closest to half the maximum starting from the peak
            if rising_edge > align_shift: # Check
                shift_start = int(rising_edge - align_shift*ssf)  # Start cut at align_shift before rising edge
                shift_end = int(shift_start + pw*ssf)  # End cut at pulsewindow after rising edge
                aligned_pulse = pulse[shift_start:shift_end]
                if len(aligned_pulse) == pw*ssf:
                    pulses_aligned.append(aligned_pulse)
                    idx_halfmax.append(rising_edge)
                    H.append(full_max) 
                else:
                    filter_diff[i] = False
            else:
                filter_diff[i] = False

            # Option to plot some pulses with their peaks, half maxima and rising edge indicated
            
            if plot_pulse:
                if below:
                    if full_max < below:
                        plot_count += 1
                else:
                    plot_count += 1
                if plot_count == every:
                    fig, ax = plt.subplots()
                    t = np.linspace(0, pw, len(pulse))
                    ax.plot(t, smoothed_pulse, lw=0.5, c='tab:orange', ls='--', label='smoothed pulse')
                    ax.scatter(t[smoothed_loc], smoothed_pulse[smoothed_loc], c='None', edgecolor='tab:orange', marker='v', label='smoothed peak')
                    ax.plot(t, pulse, lw=0.5, c='tab:blue', label='pulse')
                    ax.scatter(t[idx_max], full_max, color='None', edgecolor='tab:green', marker='v', label='peak')
                    ax.scatter(t[rising_edge], half_max, color='None', edgecolor='tab:green', marker='s', label='rising edge')
                    ax.axhline(mph, c='tab:red', lw=0.5, label='min. peak height')
                    ax.axhline(offset, c='tab:purple', lw=0.5, label='drift offset')
                    ax.set_xlabel('time [$\mu$s]')
                    ax.set_ylabel('response')
                    ax.set_xlim([0, pw])
                    ax.legend()
                    plot_count = 0
                
        pulses_aligned = np.array(pulses_aligned).reshape((-1, pw*ssf))
        idx_halfmax = np.array(idx_halfmax)
        H = np.array(H)
        sel_locs = sel_locs[filter_diff]
        locs_smoothed = locs_smoothed[filter_diff]      
        pks_smoothed = pks_smoothed[filter_diff]
        

        # Compute mean and std of aligned pulses
        mean_aligned_pulse = np.mean(pulses_aligned, axis=0)
        std_aligned_pulse = np.std(pulses_aligned, axis=0)

        # Remove outliers
        max_aligned_pulse = mean_aligned_pulse + filter_std * std_aligned_pulse
        min_aligned_pulse = mean_aligned_pulse - filter_std * std_aligned_pulse
        outliers_above = np.all(np.less(pulses_aligned, max_aligned_pulse), axis=1)
        outliers_below = np.all(np.greater(pulses_aligned, min_aligned_pulse), axis=1)
        outliers = np.logical_and(outliers_above, outliers_below)
        pulses_aligned = pulses_aligned[outliers, :]
        locs_smoothed = locs_smoothed[outliers]
        sel_locs = sel_locs[outliers]
        pks_smoothed = pks_smoothed[outliers]
        H = H[outliers]

        nr_pulses_aligned = np.shape(pulses_aligned)[0]
        nr_outliers = outliers.sum()
        perc_outliers = round(100 * (1 - nr_outliers / nr_pulses) - perc_too_close)

        # Final mean and std of aligned pulses
        mean_aligned_pulse = np.mean(pulses_aligned, axis=0)
        std_aligned_pulse = np.std(pulses_aligned, axis=0)
        perc_selected = round(100 * nr_pulses_aligned / nr_pulses)
        filtered_locs = np.setdiff1d(det_locs, locs_smoothed)
        print('N_det = %.f, N_sel = %.f (=%.f perc: -%.f perc. too close, -%.f perc. outliers)' % (nr_pulses, nr_pulses_aligned, perc_selected, perc_too_close, perc_outliers))

    return pulses_aligned, H, sel_locs, filtered_locs, pks_smoothed


def noise_model(signal, pw, sf, ssf, nr_req_segments, mph, mpp, sw):
    '''
    This function computes the average noise PSD from a given timestream
    '''
    if ssf and ssf > 1:
        pass
    else:
        ssf = 1

    # Initializing variables
    signal_length = len(signal)
    len_onesided = round(pw * ssf / 2) + 1
    sxx_segments = np.zeros(len_onesided)

    # Smooth timestream for better pulse detection
    if sw:    
        kernel = np.ones(sw) / sw
        smoothed_signal = np.convolve(signal, kernel, mode='valid')
    else:
        smoothed_signal = signal

    # Compute the average noise PSD
    nr_good_segments = 0
    start = 0
    nr = 0
    all_locs = np.array([])
    while nr_good_segments < nr_req_segments:
        start = nr * pw
        stop = start + pw
        if stop >= signal_length:  # ensure that the next segment does not run out of the available data 
            print('Not enough noise segments found with max_bw=%d (=%d)' % (pw, nr_good_segments))
            break
        next_segment = signal[start:stop]
        next_smoothed_segment = smoothed_signal[start:stop]
        locs = find_peaks(next_smoothed_segment, height=mph, prominence=mpp)[0] 
        if len(locs)!=0:  # check whether there are pulses in the data
            nr += 1
            locs += start
            all_locs = np.hstack((all_locs, locs))
        else:
            if ssf and ssf > 1:
                next_segment = supersample(next_segment, pw*ssf)
            freqs, sxx_segment = welch(next_segment, fs=sf*ssf, window='hamming', nperseg=pw*ssf, noverlap=None, nfft=None, return_onesided=True)
            # sxx_segment = psd(next_segment, sf*ssf)
            sxx_segments += sxx_segment  # cumulatively add the PSDs of all the noise segments
            nr_good_segments += 1
            nr += 1   
    if nr_good_segments == 0:
        raise Exception('No good noise segments found')
    sxx = sxx_segments / nr_good_segments  # compute the avarage PSD
    df = sf * ssf / (pw * ssf)
    # freqs = np.arange(0, sf / 2 + df, df)
    all_locs = all_locs.astype(int)
    photon_rate = len(all_locs) / (nr * pw / sf)
    return freqs, sxx, all_locs, photon_rate


def optimal_filter(pulses, pulse_model, sf, ssf, Nxx):
    ''' 
    This function applies an optimal filter, i.e. a frequency weighted filter, to the pulses to extract a better estimate of the pulse heights
    '''
    if ssf and ssf > 1:
        pass
    else:
        ssf = 1

    # Initialize important variables 
    nr_pulses, len_pulse = pulses.shape
    pw = int(len_pulse / ssf)
    len_onesided = round(pw / 2) + 1
    
    # Compute normalized pulse model
    norm_pulse_model = pulse_model / np.amax(pulse_model)

    # Step 1: compute psd and fft of normalized peak-model
    Mxx = psd(norm_pulse_model, sf*ssf, return_onesided=True)
    # _, Mxx = welch(norm_pulse_model, fs=sf*ssf, window='hamming', nperseg=pw*ssf, noverlap=None, nfft=None, return_onesided=True)
    Mf = fft(norm_pulse_model)[:len_onesided]
    Mf_conj = Mf.conj()
    Mf_abs = np.absolute(Mf)

    # Step 2: compute fft of all pulses
    Df = fft(pulses, axis=-1)[:, :len_onesided]
    Dxx = psd(pulses, sf*ssf, return_onesided=True)
    # _, Dxx = welch(pulses, fs=sf*ssf, window='hamming', nperseg=pw*ssf, noverlap=None, nfft=None, return_onesided=True, axis=1)
    mean_Dxx = np.mean(Dxx, axis=0)

    # Step 3: obtain improved pulse height estimates
    numerator = Mf_conj[:len_onesided] * Df[:, :len_onesided] / Nxx[:len_onesided]
    denominator = Mf_abs[:len_onesided]**2 / Nxx[:len_onesided]
    int_numerator = np.sum(numerator[:, 1:], axis=-1)
    int_denominator = np.sum(denominator[1:], axis=-1)
    H = np.real(int_numerator / int_denominator)

    # Step 4: compute signal-to-noise resolving power
    NEP = (np.outer((1 / H)**2, (2 * Nxx[:len_onesided] / Mxx[:len_onesided])))**0.5
    dE = 2*np.sqrt(2*np.log(2)) * (np.sum(4 / NEP[:, 1:-1]**2, axis=-1))**-0.5
    R_sn = np.mean(1 / dE)

    return H, R_sn, mean_Dxx


def psd(array, fs, return_onesided=True):
    ''' 
    This function returns the PSD estimate using the fft for either a 1D or 2D array, see https://nl.mathworks.com/help/signal/ug/power-spectral-density-estimates-using-fft.html
    '''
    # Obtain dimension of array
    ndim = array.ndim

    # For 1D arrays
    if ndim == 1:
        len_pulse = array.size
        len_onesided = round(len_pulse / 2) + 1
        fft_array = fft(array)
        fft_onesided = fft_array[:len_onesided]
        psd_array = 1 / (len_pulse * fs) * np.absolute(fft_onesided)**2
        psd_array[1:-1] *= 2
    # For 2D arrays    
    elif ndim == 2:
        len_pulse = array.shape[1]
        len_onesided = round(len_pulse / 2) + 1
        fft_array = fft(array, axis=-1)
        fft_onesided = fft_array[:, :len_onesided]
        psd_array = 1 / (len_pulse * fs) * np.absolute(fft_onesided)**2
        psd_array[:, 1:-1] *= 2
    else:  
        raise Exception("Sorry, only input n>0 pulses in a m x n array with m<=2")  
    return psd_array


def resolving_power(dist, histbin, range=None):
    ''' 
    This function obtains the resolving power of a distribution by means of a kernel density estimation
    '''
    
    # Limit the range of the KDE
    if range:
        if isinstance(range, (int, float)):
            dist = dist[dist>range]
        elif isinstance(range, (tuple, list)):
            dist = dist[(dist > range[0]) & (dist < range[1])]
        else:
            raise Exception('Please input range as integer or array-like')
        
    # Check if distribution is not empty
    if dist.size == 0:
        raise Exception('Distribution is empty, check range')
    
    # Obtain pdf of distribution
    x = np.arange(np.amin(dist), np.amax(dist), histbin/10)
    nr_peaks = dist.size
    pdfkernel = gaussian_kde(dist, bw_method='scott')
    pdf = pdfkernel.evaluate(x)

    # Obtain index, value and x-position of the maximum of the distribution
    pdf_max = np.amax(pdf)
    pdf_max_idx = np.argmax(pdf)
    x_max = x[pdf_max_idx]

    # Find the left and right index and value of the pdf at half the maximum 
    hm = pdf_max / 2
   
    idx_right = (pdf > pdf_max / 4) & (x > x_max)
    pdf_right = pdf[idx_right]
    x_right = x[idx_right]
    if np.min(pdf_right) < hm < np.max(pdf_right):
        f_right = interp1d(pdf_right, x_right)(hm)
    else:
        f_right = np.max(pdf_right)
    
    idx_left = (pdf > pdf_max / 4) & (x < x_max) & (x > x_max - 2 * (f_right - x_max))
    x_left = x[idx_left]
    pdf_left = pdf[idx_left]
    if np.min(pdf_left) < hm < np.max(pdf_left):
        f_left = interp1d(pdf_left, x_left)(hm)
    else:
        f_left = np.min(pdf_left)

    # Compute the resolving power
    resolving_power = x_max / (f_right - f_left)

    # Appropriately scale the pdf for plotting
    pdf = pdf * histbin * nr_peaks / (np.sum(pdf) * histbin/10)
        
    return resolving_power, pdf, x


def fit_decaytime(pulse, pw, fit_T):
    ''' 
    This function returns the quasiparticle regeneration time, tau_qp by fitting a function y=a*exp(-x/tau_qp) to the tail of the pulse
    '''
    # Cut the tail from the pulse for fitting
    l = len(pulse)
    ssf = int(l / pw)
    t = np.linspace(0, pw, l)

    if isinstance(fit_T, (int, float)):
        fit_pulse = pulse[t>fit_T]
        # fit_t = t[t>fit_T]
    elif isinstance(fit_T, (tuple, list)):
        fit_pulse = pulse[(t>fit_T[0]) & (t<fit_T[1])]
        # fit_t = t[(t>fit_T[0]) & (t<fit_T[1])]
    else:
        raise Exception('Please input fit_T as integer or array-like') 
    fit_x = np.arange(len(fit_pulse))

    # Obtain the optimal parameters from fit
    popt, pcov = curve_fit(exp_decay, fit_x, fit_pulse)

    # Obtain 1 std error on parameters
    perr = np.sqrt(np.diag(pcov))

    # Obtain tau_qp and error
    tau_qp = 1 / popt[1] / ssf
    dtau_qp = perr[1]

    return tau_qp, dtau_qp, popt


def exp_decay(x, a, b):
    ''' 
    This is a one-term exponential function used for aluminium KIDs: y=a*exp(-b * x)
    '''
    return a * np.exp(-b * x)


def one_over_t(x, a, b, c):
    ''' 
    This is the 1/t exponential function used for bTa KIDs: y=a / ((1 + c) * exp(-b * x) - 1)
    '''
    return a / ((1 + c) * np.exp(b * x) - 1)


def get_window(type, M, tau):
    ratio = 0.01
    if type == 'box':
        M = tau
        y = windows.boxcar(M) / M
    if type == 'exp':
        M = int(-tau*np.log(ratio))
        x = np.linspace(0, M-1, M)
        y = exp_decay(x, 1, 1 / tau)
        y /= np.sum(y)
    if type == '1/t':
        M = int(1 / ratio - 1)
        x = np.linspace(0, M-1, M)
        y  = 1 / (1 + x)
        y /= np.sum(y)
    # fig, ax = plt.subplots()
    # ax.set_title('Window for smoothing')
    # ax.plot(y)
    # ax.set_ylim([0, 1])
    # plt.show()
    return y


def plot_some_pulses(mkid, dimx, dimy, save=False, ylim=None):
    fig, axes = plt.subplots(dimx, dimy, layout='constrained', sharey=True, sharex=True)
    fig.suptitle('Some pulses: ' + mkid.data['name'])
    pulses = mkid.data['pulses']
    nr_pulses = pulses.shape[0]
    i = 0
    for ax in axes.flatten():
        if i < nr_pulses:
            pulse = pulses[i, :]
            ax.plot(pulses[i, :], lw=0.2)
            i += 1
        else:
            break
    if ylim:
        _ = ax.set_ylim([ylim])
    _ = ax.set_xlim([0, len(pulse)])
    _ = fig.supxlabel('t [$\mu$s]')
    _ = fig.supylabel('$\\theta$ [rad]')
    if save:
        figpath = r'C:/Users/wilbertr/OneDrive/TU Delft/PhD/Data analysis/MIR/Smith/'
        fname = figpath + mkid.data['name']
        plt.savefig(fname + '_pulses.png')
        plt.savefig(fname + '_pulses.svg')


def get_kid(dir_path, lt, wl, kid, date):
    data = '%sLT%s_%snm_KID%s*%s_data.txt' % (dir_path, str(lt), str(wl), str(kid), str(date))
    settings = '%sLT%s_%snm_KID%s*%s_settings.txt' % (dir_path, str(lt), str(wl), str(kid), str(date))
    data_path = glob.glob(data)
    settings_path = glob.glob(settings)
    if not data_path:
        raise Exception('Please correct kid path as no file was obtained: %s' % (data))
    if len(data_path) == 1:
        with open(data_path[0], 'rb') as file:
            kid = pickle.load(file)
        with open(settings_path[0], 'r') as file:
            settings = {}
            for line in file:
                (key, val) = re.split(":", line)
                kid[key] = val[:-1]                
    else:
        raise Exception('Multiple kids detected')
    return kid

