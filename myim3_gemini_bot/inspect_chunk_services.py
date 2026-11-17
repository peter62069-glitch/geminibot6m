import requests
import re

resp = requests.get('https://simxpro.org/_next/static/chunks/616-759d2af99f3ef672.js')
text = resp.text

for m in re.finditer(r'getServices', text):
    idx = m.start()
    print('--- getServices context ---')
    print(text[max(0, idx-100):min(len(text), idx+400)])

