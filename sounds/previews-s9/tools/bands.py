import sys, subprocess, numpy as np
from scipy import signal
for f in sys.argv[1:]:
    x = np.frombuffer(subprocess.run(['ffmpeg','-v','error','-i',f,'-ac','1','-ar','44100','-f','f32le','-'],capture_output=True).stdout, np.float32)
    fr, p = signal.welch(x, 44100, nperseg=8192)
    tot = p.sum()
    b = [(0,120),(120,500),(500,2000),(2000,8000),(8000,22050)]
    s = ' '.join(f'{lo}-{hi}:{100*p[(fr>=lo)&(fr<hi)].sum()/tot:4.1f}%' for lo,hi in b)
    # tonal peaks
    lp = 10*np.log10(p+1e-20); pk,pr = signal.find_peaks(lp, prominence=12)
    tones = [f'{fr[i]:.0f}Hz' for i in pk[np.argsort(-pr['prominences'])][:3] if fr[i]>60]
    print(f'{f.split("/")[-1]:22s} {s}  tones:{tones}')
