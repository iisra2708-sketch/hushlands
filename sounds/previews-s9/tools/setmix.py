"""setmix.py <world> <mix.yaml>  -> replaces audio: block and target_rms_db in worlds/<world>.yaml (text-level)"""
import sys, re, yaml
w, mixf = sys.argv[1], sys.argv[2]
p = f'/home/claude/hushlands/worlds/{w}.yaml'
s = open(p).read()
mixtxt = open(mixf).read()
tgt = re.search(r'^target_rms_db:\s*(\S+)', mixtxt, re.M).group(1)
layers = mixtxt[mixtxt.index('layers:') + len('layers:'):].rstrip('\n')
s = re.sub(r'^target_rms_db:.*\n', '', s, flags=re.M)
s = re.sub(r'^audio:\n(?:  .*\n)+', f'target_rms_db: {tgt}\naudio:{layers}\n', s, flags=re.M)
yaml.safe_load(s)
open(p, 'w').write(s)
print(w, 'ok')
