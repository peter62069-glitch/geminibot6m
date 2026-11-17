import requests
import re

r = requests.get('https://uotp.store', headers={'User-Agent': 'Mozilla/5.0'})
chunks = re.findall(r'/_next/static/chunks/[a-zA-Z0-9_\-\.~]+\.js', r.text)

print(f'Checking {len(chunks)} chunks...')
for c in chunks:
    try:
        t = requests.get('https://uotp.store' + c).text
        for term in ['handler_api', 'getNumber', 'api_key', 'myim3', 'indosat']:
            if term.lower() in t.lower():
                print(f'Match in {c} for {term}!')
                idx = t.lower().find(term.lower())
                print(t[max(0, idx-60):min(len(t), idx+120)])
    except Exception as e:
        pass
print('Done!')

