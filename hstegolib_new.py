#!/usr/bin/env python3

import os
import sys
import glob
import struct
import base64
import imageio
import hashlib

import scipy.signal
import scipy.fftpack
import numpy as np

from ctypes import *
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import pad, unpad

MAX_PAYLOAD=0.05
INF = 2**31-1

__file__ = "/usr/local/lib/python3.6/dist-packages/hstegolib_new.py" # XXX

jpg_candidates = glob.glob(os.path.join(os.path.dirname(__file__), 'hstego_jpeg_toolbox_extension.*.so'))
if not jpg_candidates:
    print("JPEG Toolbox library not found:", os.path.dirname(__file__))
    sys.exit(0)
jpeg = CDLL(jpg_candidates[0])

stc_candidates = glob.glob(os.path.join(os.path.dirname(__file__), 'hstego_stc_extension.*.so'))
if not stc_candidates:
    print("STC library not found:", os.path.dirname(__file__))
    sys.exit(0)
stc = CDLL(stc_candidates[0])



class Cipher:
    # {{{
    def __init__(self, password):
        self.password = password

    def open_file(self, path):
        with open(path, 'rb') as f:
            self.plaintext = f.read()

    def aes_encrypt(self):
        salt = get_random_bytes(AES.block_size)

        # use the Scrypt KDF to get a private key from the password
        private_key = hashlib.scrypt(
            self.password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)

        cipher = AES.new(private_key, AES.MODE_CBC)
        ciphertext = cipher.encrypt(pad(self.plaintext, AES.block_size))

        # Ciphertext with a 16+16 header 
        self.ciphertext = salt + cipher.iv + ciphertext

    def encrypt(self, input_path):
        """ encrypt file content """
        self.open_file(input_path)
        self.aes_encrypt()
        return self.ciphertext
  
    def aes_decrypt(self):
        salt = self.ciphertext[:AES.block_size]
        iv = self.ciphertext[AES.block_size:AES.block_size*2]
        ciphertext = self.ciphertext[AES.block_size*2:]

        # Fix padding
        mxlen = len(ciphertext)-(len(ciphertext)%AES.block_size)
        ciphertext = ciphertext[:mxlen]

        private_key = hashlib.scrypt(
            self.password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)

        cipher = AES.new(private_key, AES.MODE_CBC, iv=iv)
        decrypted = cipher.decrypt(ciphertext)
        self.decrypted = unpad(decrypted, AES.block_size)

    def decrypt(self, bytes_array, output_path):
        """ decrypt """
        self.ciphertext = bytes_array
        self.aes_decrypt()

        with open(output_path, 'wb') as f:
            f.write(bytes(self.decrypted))
    # }}}

class Stego:
    # {{{
    def __init__(self):
        pass

    def bytes_to_bits(self, data):
        array=[]
        for b in data:
            for i in range(8):
                array.append((b >> i) & 1)
        return array


    def hide_stc(self, cover_array, costs_array, message_bits, mx=255, mn=0):

        cover = (c_int*(len(cover_array)))()
        for i in range(len(cover_array)):
            cover[i] = int(cover_array[i])

        # Prepare costs
        costs = (c_float*(len(costs_array)*3))()
        for i in range(len(costs_array)):
            if cover[i]<=mn:
                costs[3*i+0] = INF
                costs[3*i+1] = 0
                costs[3*i+2] = costs_array[i]
            elif cover[i]>=mx:
                costs[3*i+0] = costs_array[i]
                costs[3*i+1] = 0 
                costs[3*i+2] = INF
            else:
                costs[3*i+0] = costs_array[i]
                costs[3*i+1] = 0
                costs[3*i+2] = costs_array[i]


        m = len(message_bits)
        message = (c_ubyte*m)()
        for i in range(m):
            message[i] = message_bits[i]

        # Hide message
        stego = (c_int*(len(cover_array)))()
        _ = stc.stc_hide(len(cover_array), cover, costs, m, message, stego)

        # stego data to numpy
        stego_array = cover_array.copy()
        for i in range(len(cover_array)):
            stego_array[i] = stego[i]
     
        return stego_array


    def hide(self, message, cover_matrix, cost_matrix, mx=255, mn=0):
        message_bits = self.bytes_to_bits(message)

        height, width = cover_matrix.shape
        cover_array = cover_matrix.reshape((height*width,)) 
        costs_array = cost_matrix.reshape((height*width,)) 

        # Hide data_len (32 bits) into 64 pixels (0.5 payload)
        data_len = struct.pack("!I", len(message_bits))
        data_len_bits = self.bytes_to_bits(data_len)

        stego_array_1 = self.hide_stc(cover_array[:64], costs_array[:64], data_len_bits, mx, mn)
        stego_array_2 = self.hide_stc(cover_array[64:], costs_array[64:], message_bits, mx, mn)
        stego_array = np.hstack((stego_array_1, stego_array_2))

        stego_matrix = stego_array.reshape((height, width))
        
        return stego_matrix


    def unhide_stc(self, stego_array, message_len):

        stego = (c_int*(len(stego_array)))()
        for i in range(len(stego_array)):
            stego[i] = int(stego_array[i])

        extracted_message = (c_ubyte*len(stego_array))()
        s = stc.stc_unhide(len(stego_array), stego, message_len, extracted_message)


        # Message bits to bytes
        data = bytearray()
        idx=0
        bitidx=0
        bitval=0
        for i in range(message_len):
            if bitidx==8:
                data.append(bitval)
                bitidx=0
                bitval=0
            bitval |= extracted_message[i]<<bitidx
            bitidx+=1
            idx += 1
        if bitidx==8:
            data.append(bitval)

        data = bytes(data)
        return data


    def unhide(self, stego_matrix):

        height, width = stego_matrix.shape
        stego_array = stego_matrix.reshape((height*width,))

        # Extract a 32-bits message lenght from a 64-pixel array
        data = self.unhide_stc(stego_array[:64], 32)
        data_len = struct.unpack_from("!I", data, 0)[0]
        
        data = self.unhide_stc(stego_array[64:], data_len)
        return data

    # }}}



# {{{ HILL()
def HILL(I):                                                                
    HF1 = np.array([                                                             
        [-1, 2, -1],                                                             
        [ 2,-4,  2],                                                             
        [-1, 2, -1]                                                              
    ])                                                                           
    H2 = np.ones((3, 3)).astype(np.float)/3**2                                   
    HW = np.ones((15, 15)).astype(np.float)/15**2                                
                                                                                 
    R1 = scipy.signal.convolve2d(I, HF1, mode='same', boundary='symm')
    W1 = scipy.signal.convolve2d(np.abs(R1), H2, mode='same', boundary='symm')
    rho=1./(W1+10**(-10))
    cost = scipy.signal.convolve2d(rho, HW, mode='same', boundary='symm')

    cost[np.isnan(cost)] = INF
    cost[cost>INF] = INF

    return cost     
# }}}

# {{{ J_UNIWARD()
def dct2(a):
    return scipy.fftpack.dct(scipy.fftpack.dct( a, axis=0, norm='ortho' ), axis=1, norm='ortho')
    
def idct2(a):
    return scipy.fftpack.idct(scipy.fftpack.idct( a, axis=0 , norm='ortho'), axis=1 , norm='ortho')

def J_UNIWARD(coef_arrays, quant_tables, spatial):

    hpdf = np.array([
        -0.0544158422,  0.3128715909, -0.6756307363,  0.5853546837,  
         0.0158291053, -0.2840155430, -0.0004724846,  0.1287474266,  
         0.0173693010, -0.0440882539, -0.0139810279,  0.0087460940,  
         0.0048703530, -0.0003917404, -0.0006754494, -0.0001174768
    ])        

    sign = np.array([-1 if i%2 else 1 for i in range(len(hpdf))])
    lpdf = hpdf[::-1] * sign

    F = []
    F.append(np.outer(lpdf.T, hpdf))
    F.append(np.outer(hpdf.T, lpdf))
    F.append(np.outer(hpdf.T, hpdf))


    # Pre-compute impact in spatial domain when a jpeg coefficient is changed by 1
    spatial_impact = {}
    for i in range(8):
        for j in range(8):
            test_coeffs = np.zeros((8, 8))
            test_coeffs[i, j] = 1
            spatial_impact[i, j] = idct2(test_coeffs) * quant_tables[i, j]

    # Pre compute impact on wavelet coefficients when a jpeg coefficient is changed by 1
    wavelet_impact = {}
    for f_index in range(len(F)):
        for i in range(8):
            for j in range(8):
                wavelet_impact[f_index, i, j] = scipy.signal.correlate2d(spatial_impact[i, j], F[f_index], mode='full', boundary='fill', fillvalue=0.) # XXX


    # Create reference cover wavelet coefficients (LH, HL, HH)
    pad_size = 16 # XXX
    spatial_padded = np.pad(spatial, (pad_size, pad_size), 'symmetric')


    RC = []
    for i in range(len(F)):
        f = scipy.signal.correlate2d(spatial_padded, F[i], mode='same', boundary='fill')
        RC.append(f)

    coeffs = coef_arrays
    k, l = coeffs.shape
    nzAC = np.count_nonzero(coef_arrays) - np.count_nonzero(coef_arrays[::8, ::8])

    rho = np.zeros((k, l))
    tempXi = [0.]*3
    sgm = 2**(-6)

    # Computation of costs
    for row in range(k):
        for col in range(l):
            mod_row = row % 8
            mod_col = col % 8
            sub_rows = list(range(row-mod_row-6+pad_size-1, row-mod_row+16+pad_size))
            sub_cols = list(range(col-mod_col-6+pad_size-1, col-mod_col+16+pad_size))

            for f_index in range(3):
                RC_sub = RC[f_index][sub_rows][:,sub_cols]
                wav_cover_stego_diff = wavelet_impact[f_index, mod_row, mod_col]
                tempXi[f_index] = abs(wav_cover_stego_diff) / (abs(RC_sub)+sgm)

            rho_temp = tempXi[0] + tempXi[1] + tempXi[2]
            rho[row, col] = np.sum(rho_temp)


    rho[np.isnan(rho)] = INF
    rho[rho>INF] = INF

    return rho
# }}}





cover = "testing/cover_color.png"
path = "testing/input-secret-small.txt"
password = "p4ssw0rd"


with open(path, 'rb') as f:
    print("real len:", len(f.read()))

# Hide
C = imageio.imread(cover)

cipher = Cipher(password)
message = cipher.encrypt(path)
print("enc len:", len(message))


stego = Stego()
S = np.zeros(C.shape)
S[:,:,0] = stego.hide(message, C[:,:,0], HILL(C[:,:,0])) 




# Unhide
print("Unhide!")
C2 = np.zeros(S.shape)
extracted = stego.unhide(S[:,:,0])
print("Extracted:", extracted)
cipher.decrypt(extracted, "output.txt")








