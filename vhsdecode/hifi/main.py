#!/usr/bin/env python3
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import cpu_count
from datetime import datetime
import os
import sys
import soundfile as sf

from vhsdecode.cmdcommons import (
    common_parser_cli,
    select_sample_freq,
    select_system,
    get_basics
)
from vhsdecode.hifi.HiFiDecode import HiFiDecode, NoiseReduction
from vhsdecode.hifi.TimeProgressBar import TimeProgressBar


parser, _ = common_parser_cli("Extracts audio from raw VHS HiFi FM capture", default_threads=round(cpu_count() / 2) + 1)

parser.add_argument(
    "--bg", dest="BG", action="store_true", default=False, help='Do carrier bias guess'
)

parser.add_argument(
    "--preview", dest="preview", action="store_true", default=False, help='Use preview quality (faster and noisier)'
)

parser.add_argument(
    "--original", dest="original", action="store_true", default=False, help='Use the same FM demod as vhs-decode'
)

parser.add_argument(
    "--noise_reduction", dest="noise_reduction", type=str.lower, default='on',
    help='Set noise reduction on/off'
)

parser.add_argument(
    "--auto_fine_tune", dest="auto_fine_tune", type=str.lower, default='on',
    help='Set auto tuning of the analog front end on/off'
)

DEFAULT_NR_GAIN_ = 33

parser.add_argument(
    "--NR_sidechain_gain", dest="NR_side_gain", type=float, default=DEFAULT_NR_GAIN_,
    help=f'Sets the noise reduction envelope tracking sidechain gain (default is {DEFAULT_NR_GAIN_}). '
         f'Range (20~100): 100 being a hard gate effect, operating range should be 40 and below'
)

parser.add_argument(
    "--h8", dest="H8", action="store_true", default=False, help='8mm/Hi8 tape format'
)

parser.add_argument(
    "--gnuradio", dest="GRC", action="store_true", default=False, help='Opens ZMQ REP pipe to gnuradio at port 5555'
)

args = parser.parse_args()
filename, outname, _, _ = get_basics(args)
system = select_system(args)
sample_freq = select_sample_freq(args)

options = {
    'input_rate': sample_freq * 1e6,
    'standard': 'p' if system == 'PAL' else 'n',
    'format': 'vhs' if not args.H8 else 'h8',
    'preview': args.preview,
    'original': args.original,
    'noise_reduction': args.noise_reduction == 'on' if not args.preview else False,
    'auto_fine_tune': args.auto_fine_tune == 'on' if not args.preview else False,
    'nr_side_gain': args.NR_side_gain,
    'grc': args.GRC
}


# This part is what opens the file
# The samplerate here could be anything
def as_soundfile(pathR, sample_rate=44100):
    path = pathR.lower()
    if '.raw' in path or '.s16' in path:
        return sf.SoundFile(pathR, 'r', channels=1, samplerate=int(sample_rate), format='RAW', subtype='PCM_16',
                            endian='LITTLE')
    elif '.u8' in path or '.r8' in path:
        return sf.SoundFile(pathR, 'r', channels=1, samplerate=int(sample_rate), format='RAW', subtype='PCM_U8',
                            endian='LITTLE')
    elif '.s8' in path:
        return sf.SoundFile(pathR, 'r', channels=1, samplerate=int(sample_rate), format='RAW', subtype='PCM_S8',
                            endian='LITTLE')
    elif '.u16' in path or '.r16' in path:
        sys.exit('Unsigned 16 bit raw not supported yet')
    elif '.ogx' in path:
        sys.exit('OGX container not supported yet, try converting it to flac')
    else:
        return sf.SoundFile(pathR, 'r')


def as_outputfile(path, sample_rate):
    if '.wav' in path.lower():
        return sf.SoundFile(path, 'w', channels=2, samplerate=int(sample_rate), format='WAV', subtype='PCM_16')
    else:
        return sf.SoundFile(path, 'w', channels=2, samplerate=int(sample_rate), format='FLAC', subtype='PCM_24')


def log_decode_speed(start_time, frames):
    elapsed_time = datetime.now() - start_time
    print(f'Decoding speed: {round(frames / (1e3 * elapsed_time.total_seconds()))} kFrames/s')


def decode(decoder, input_file, output_file):
    start_time = datetime.now()
    noise_reduction = NoiseReduction(decoder.notchFreq, args.NR_side_gain, decoder.audioDiscard)
    with as_outputfile(output_file, decoder.audioRate) as w:
        with as_soundfile(input_file) as f:
            progressB = TimeProgressBar(f.frames, f.frames)
            current_block = 0
            for block in f.blocks(blocksize=decoder.blockSize, overlap=decoder.readOverlap):
                progressB.print(f.tell())
                current_block, audioL, audioR = decoder.block_decode(block, block_count=current_block)

                if options['noise_reduction']:
                    stereo = noise_reduction.stereo(audioL, audioR)
                else:
                    stereo = list(map(list, zip(audioL, audioR)))

                if options['auto_fine_tune']:
                    log_bias(decoder)

                log_decode_speed(start_time, f.tell())
                w.write(stereo)

        elapsed_time = datetime.now() - start_time
        dt_string = elapsed_time.total_seconds()
        print(f'\nDecode finished, seconds elapsed: {round(dt_string)}')


def decode_parallel(decoders, input_file, output_file, threads=8):
    start_time = datetime.now()
    audio_rate = decoders[0].audioRate
    block_size = decoders[0].blockSize
    read_overlap = decoders[0].readOverlap
    noise_reduction = NoiseReduction(decoders[0].notchFreq, args.NR_side_gain, decoders[0].audioDiscard)
    futures_queue = list()
    executor = ThreadPoolExecutor(threads)
    current_block = 0
    with as_outputfile(output_file, audio_rate) as w:
        with as_soundfile(input_file) as f:
            progressB = TimeProgressBar(f.frames, f.frames)
            for block in f.blocks(blocksize=block_size, overlap=read_overlap):
                futures_queue.append(
                    executor.submit(
                        decoders[current_block % threads].block_decode, block, current_block
                    )
                )
                if options['auto_fine_tune']:
                    log_bias(decoders[current_block % threads])

                current_block += 1
                progressB.print(f.tell())

                while len(futures_queue) > threads:
                    future = futures_queue.pop(0)
                    blocknum, audioL, audioR = future.result()
                    if options['noise_reduction']:
                        stereo = noise_reduction.stereo(audioL, audioR)
                    else:
                        stereo = list(map(list, zip(audioL, audioR)))
                    log_decode_speed(start_time, f.tell())
                    w.write(stereo)

            print('Emptying the decode queue ...')
            while len(futures_queue) > 0:
                future = futures_queue.pop(0)
                blocknum, audioL, audioR = future.result()
                if options['noise_reduction']:
                    stereo = noise_reduction.stereo(audioL, audioR)
                else:
                    stereo = list(map(list, zip(audioL, audioR)))
                log_decode_speed(start_time, f.tell())
                w.write(stereo)

        elapsed_time = datetime.now() - start_time
        dt_string = elapsed_time.total_seconds()
        print(f'\nDecode finished, seconds elapsed: {round(dt_string)}')


def guess_bias(decoder, input_file, block_size, blocks_limits=10):
    print("Measuring carrier bias ... ")
    blocks = list()

    with as_soundfile(input_file) as f:
        while f.tell() < f.frames and len(blocks) <= blocks_limits:
            blocks.append(f.read(block_size))

    LCRef, RCRef = decoder.guessBiases(blocks)
    print("done!")
    print("L carrier found at %.02f MHz, R carrier found at %.02f MHz" %
          (LCRef / 1e6, RCRef / 1e6)
          )
    return LCRef, RCRef


def log_bias(decoder):
    devL = (decoder.standard.LCarrierRef - decoder.afe_params.LCarrierRef) / 1e3
    devR = (decoder.standard.RCarrierRef - decoder.afe_params.RCarrierRef) / 1e3
    print("Bias L %.02f kHz, R %.02f kHz" % (devL, devR), end=' ')
    if abs(devL) < 9 and abs(devR) < 9:
        print("(good player/recorder calibration)")
    elif 9 <= abs(devL) < 10 or 9 <= abs(devR) < 10:
        print("(maybe marginal player/recorder calibration)")
    else:
        print("\nWARN: the player or the recorder may be uncalibrated and/or\n"
              "the standard and/or the sample rate specified are wrong")


def main():
    if not args.overwrite:
        if os.path.isfile(outname):
            print("Existing decode files found, remove them or run command with --overwrite")
            print("\t", outname)
            sys.exit(1)

    print("Initializing ...")
    if options['format'] == 'vhs':
        print('PAL VHS format selected') if system == 'PAL' else print('NTSC VHS format selected')
    else:
        print('NTSC Hi8 format selected')

    if sample_freq is not None:
        decoder = HiFiDecode(options)
        LCRef, RCRef = decoder.standard.LCarrierRef, decoder.standard.RCarrierRef
        if args.BG:
            LCRef, RCRef = guess_bias(decoder, filename, decoder.blockSize)
            decoder.updateAFE(LCRef, RCRef)

        if args.threads > 1 and not args.GRC:
            decoders = list()
            for i in range(0, args.threads):
                decoders.append(HiFiDecode(options))
                decoders[i].updateAFE(LCRef, RCRef)
            decode_parallel(decoders, filename, outname, threads=args.threads)
        else:
            decode(decoder, filename, outname)
    else:
        print('No sample rate specified')
        sys.exit(0)


if __name__ == "__main__":
    main()
