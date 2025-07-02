#!/usr/bin/python
#
# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import hvac
import os

# Constants
VAULT_URL = 'https://vault.vault:8200'
SECRET_PATH = 'secret/data/prod/sendgrid'
SERVICE_ACCOUNT_TOKEN_PATH = '/var/run/secrets/kubernetes.io/serviceaccount/token'

def get_sendgrid_secret(logger):
    """
    Retrieves SendGrid API key from HashiCorp Vault using Kubernetes authentication
    
    Args:
        logger: Logger instance for logging operations
        
    Returns:
        str: The SendGrid API key
        
    Raises:
        Exception: If unable to retrieve the secret from Vault
    """
    try:
        logger.info('Attempting to fetch SendGrid secret from Vault...')
        
        # Get the Kubernetes namespace for the vault role
        namespace = os.environ.get('KUBERNETES_NAMESPACE', 'default')
        vault_role = f'{namespace}-emailservice'
        
        # Read Kubernetes service account token
        with open(SERVICE_ACCOUNT_TOKEN_PATH, 'r') as f:
            jwt = f.read().strip()
        
        # Initialize Vault client
        client = hvac.Client(
            url=VAULT_URL,
            verify=False  # Skip TLS certificate validation
        )
        
        logger.info(f'Authenticating with Vault using role: {vault_role}')
        
        # Authenticate to Vault using Kubernetes auth method
        auth_response = client.auth.kubernetes.login(
            role=vault_role,
            jwt=jwt
        )
        
        logger.info(f'Fetching secret from path: {SECRET_PATH}')
        
        # Fetch the secret
        secret_response = client.secrets.kv.v2.read_secret_version(
            path='prod/sendgrid',
            mount_point='secret'
        )
        
        # In KV-v2 secrets engine, the actual data is nested under data.data
        if not secret_response or 'data' not in secret_response or 'data' not in secret_response['data']:
            raise Exception('Secret response did not contain expected data structure')
        
        secret_data = secret_response['data']['data']
        
        if 'api_key' not in secret_data:
            raise Exception('Secret response did not contain expected api_key field')
        
        logger.info('Successfully retrieved SendGrid API key from Vault')
        
        return secret_data['api_key']
        
    except Exception as error:
        raise Exception(f'Error retrieving secret from Vault: {str(error)}')