"""Check the HTML's actual math against SciPy. Run: python mockup/verify_math.py.
Requires Node.js and SciPy, which the bot already uses.
"""
import json
import subprocess
from pathlib import Path
from scipy.stats import norm, truncnorm

html = Path(__file__).with_name('index.html').read_text(encoding='utf-8')
js = html.split('<script>')[1].split('</script>')[0]
syntax = subprocess.run(['node', '--check'], input=js, text=True, encoding="utf-8", capture_output=True)
assert syntax.returncode == 0, syntax.stderr
# Extract pure functions directly from the deliverable, without a DOM or hardware.
math = js[js.index('function phi'):js.index('const pct=')]
config = dict(REEL_TIME_MIN=.985, REEL_TIME_MAX=4,
              REEL_TIME_TAIL_PROBABILITY=.0005, REEL_TIME_MEAN_MAX_MODIFIER=.4,
              CAST_TIME_MIN=.985, CAST_TIME_MAX=3, CAST_TIME_TAIL_PROBABILITY=.0000005)
xs = [-6, -2, 0, .7, 1.86, 3, 5, 6, 8]
ps = [.00025, .00000025, 5e-11, .45]
cutoffs = [0, .985, 1, 1.5, 2.1, 4, 8]
script = f'const cfg={json.dumps(config)}\n' + math + f'''
const r=model('REEL_TIME'),c=model('CAST_TIME',1.4)
console.log(JSON.stringify({{
 sf:{json.dumps(xs)}.map(sf),z:{json.dumps(ps)}.map(isf),
 skips:{json.dumps(cutoffs)}.map(r.skip),mean:r.mean,
 castMean:c.mean,castTail:c.skip(3)
}}))
'''
result = json.loads(subprocess.check_output(['node', '-e', script], text=True))
assert max(abs(v-norm.sf(x)) for v, x in zip(result['sf'], xs)) < 1e-9
assert max(abs(v-norm.isf(p)) for v, p in zip(result['z'], ps)) < 1e-7
mu = (.985+4*.4)/2
sigma = (4-.985)/(2*norm.isf(.0005/2))
r = truncnorm((.985-mu)/sigma, float('inf'), loc=mu, scale=sigma)
assert max(abs(v-r.sf(x)) for v, x in zip(result['skips'], cutoffs)) < 1e-8
assert abs(result['mean']-r.mean()) < 1e-8
sigma_c = (3-.985)/(2*norm.isf(.0000005/2))
c = truncnorm((.985-1.4)/sigma_c, float('inf'), loc=1.4, scale=sigma_c)
assert abs(result['castMean']-c.mean()) < 1e-8
assert abs(result['castTail']-c.sf(3)) < 1e-12
print('PASS: JS syntax, survival function, inverse survival, truncated means, cutoff boundaries, conditional cast tail.')
print(f'Default cutoff: {r.sf(2.1)*100:.6f}% skipped. Detection age 25 s: {r.sf(1.5)*100:.6f}% skipped.')
