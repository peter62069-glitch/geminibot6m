import requests

key = 'fS5DlwcHNaXyvE3gFMXh76N1vs5OBkCn0yLifWs0'
url = 'https://simxpro.org/api/stubs/handler_api.php'

# Test services with operator 31 and operator 7
services = ['im', 'is', 'mi', 'it', 'mo', 'ot', 'im3', 'myim3', 'indosat']

for s in services:
    for op in ['31', '7']:
        r = requests.get(url, params={'api_key': key, 'action': 'getNumber', 'service': s, 'country': '6', 'operator': op}, timeout=5)
        text = r.text.strip()
        print(f"service={s}, operator={op} -> {text}")
        if 'ACCESS_NUMBER' in text:
            # cancel it right away
            oid = text.split(':')[1]
            print(f"   Bought {oid}, canceling...")
