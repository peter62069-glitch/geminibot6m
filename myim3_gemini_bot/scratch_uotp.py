import requests
import re

r = requests.get('https://uotp.store', headers={'User-Agent': 'Mozilla/5.0'})
chunks = re.findall(r'/_next/static/chunks/[^"]+\.js', r.text)
print('Found chunks:', len(chunks))

for c in chunks:
    url = 'https://uotp.store' + c
    js_text = requests.get(url).text
    for term in ['9.55', '13.91', 'indosat', 'myim3']:
        if term in js_text.lower():
            print(f'Chunk {c} contains {term}!')
            idx = js_text.lower().find(term)
            print(js_text[max(0, idx-60):min(len(js_text), idx+100)])

