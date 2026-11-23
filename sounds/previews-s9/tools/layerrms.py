import sys, yaml, numpy as np, zlib
sys.path.insert(0,'/home/claude/hushlands')
from pipeline.audio import AudioSpec, build_layers, SR
mix=yaml.safe_load(open(sys.argv[1])); seed=zlib.crc32(sys.argv[2].encode())%100000
L=build_layers(AudioSpec(layers=mix['layers'],seconds=120,seed=seed,target_rms_db=-30))
n=int(30*SR); outs=[np.zeros((2,0))]*len(L)
for c in range(4):
    t=(c*n+np.arange(n))/SR
    outs=[np.concatenate([o,l(n,t)],axis=1) for o,l in zip(outs,L)]
for ld,o in zip(mix['layers'],outs):
    r=np.sqrt(np.mean(o**2)); p=np.abs(o).max()
    print(f"{ld['type']:10s} {ld.get('pool','')[:16]:16s} rms {20*np.log10(r+1e-12):6.1f} dB  peak {20*np.log10(p+1e-12):6.1f} dB")
