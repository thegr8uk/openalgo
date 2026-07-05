import os
import sys
import json
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app

@pytest.fixture
def client():
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_assetlinks_json_endpoint(client):
    response = client.get('/.well-known/assetlinks.json')
    assert response.status_code == 200
    assert response.mimetype == 'application/json'
    
    # Read the expected content from the root assetlinks.json
    with open('assetlinks.json', 'r') as f:
        expected_data = json.load(f)
        
    data = json.loads(response.data)
    assert data == expected_data
