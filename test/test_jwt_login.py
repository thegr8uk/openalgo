import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app

@pytest.fixture
def client():
    app = create_app()
    app.config['TESTING'] = True
    # Ensure cookie configuration defaults for testing
    app.config['SESSION_COOKIE_NAME'] = 'session'
    with app.test_client() as client:
        yield client

def test_auth_jwt_missing_parameter(client):
    """If no 'jwt' parameter is provided, redirect to /login."""
    response = client.get('/auth')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/login')

def test_auth_jwt_success(client):
    """If 'jwt' parameter is provided, redirect to /dashboard and set cookie."""
    test_token = "dummy_jwt_token_payload"
    response = client.get(f'/auth?jwt={test_token}')
    
    # Assert redirect to dashboard
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/dashboard')
    
    # Assert session cookie is set
    cookie_header = response.headers.get('Set-Cookie', '')
    assert 'session=' in cookie_header
    assert test_token in cookie_header
