import requests

key = 'PG0wnEUis2v9AJagtKghxtbvIhiNlOwe1yWUDMKZ'
url = 'https://uotp.store/api/stubs/handler_api.php'

# Check operators with service=is, im3, indosat, myim3
with open('uotp_results.txt', 'w', encoding='utf-8') as f:
    for s in ['is', 'im3', 'myim3', 'indosat']:
        f.write(f'=== Testing service {s} ===\n')
        for op in range(1, 35):
            try:
                r = requests.get(url, params={'api_key': key, 'action': 'getNumber', 'service': s, 'country': '6', 'operator': str(op)}, timeout=3)
                t = r.text.strip()
                if t != 'BAD_OPERATOR' and t != 'BAD_SERVICE':
                    f.write(f'service={s}, op={op} -> {t}\n')
                    f.flush()
                    if 'ACCESS_NUMBER' in t:
                        oid = t.split(':')[1]
                        requests.get(url, params={'api_key': key, 'action': 'setStatus', 'status': '8', 'id': oid})
            except Exception as e:
                pass
f.write('DONE\n')

