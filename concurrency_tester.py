import requests
from concurrent.futures import ThreadPoolExecutor

URL = "http://127.0.0.1:8000/products/reserve/"

requests_data = [
    {
        "product_id": 1,
        "quantity": 1,
        "transaction_id": "11111111-1111-1111-1111-111111111111",
    },
    {
        "product_id": 1,
        "quantity": 1,
        "transaction_id": "22222222-2222-2222-2222-222222222222",
    },
]


def reserve(data):
    response = requests.post(URL, json=data)
    return response.status_code, response.json()


with ThreadPoolExecutor(max_workers=2) as executor:
    results = list(executor.map(reserve, requests_data))


for result in results:
    print(result)