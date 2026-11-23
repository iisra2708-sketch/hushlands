"""Preview de audio: python prev.py <mix.yaml> <name> [seconds] [seed]
mix.yaml: {target_rms_db: -29, layers: [...]}"""
import sys, yaml, subprocess
sys.path.insert(0, '/home/claude/hushlands')
import os; os.chdir('/home/claude/hushlands')
from pipeline.audio import AudioSpec, render_audio
mix = yaml.safe_load(open(sys.argv[1]))
name = sys.argv[2]; secs = float(sys.argv[3]) if len(sys.argv) > 3 else 150; seed = int(sys.argv[4]) if len(sys.argv) > 4 else 1
out = f'/home/claude/prev/{name}.m4a'
render_audio(AudioSpec(layers=mix['layers'], seconds=secs, seed=seed, target_rms_db=mix.get('target_rms_db', -29)), out, log=lambda *a: None)
r = subprocess.run(['ffmpeg', '-hide_banner', '-i', out, '-af', 'ebur128=peak=true', '-f', 'null', '-'], capture_output=True, text=True).stderr
I = [l for l in r.splitlines() if l.strip().startswith('I:')][-1].split()[1]
P = [l for l in r.splitlines() if 'Peak:' in l][-1].split()[1]
print(f'{name}: {I} LUFS, peak {P} dBFS -> {out}')
