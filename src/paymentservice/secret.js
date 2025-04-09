const vault = require('node-vault');
const fs = require('fs');

// Constants
const NAMESPACE = process.env.KUBERNETES_NAMESPACE || 'default'; 
const VAULT_URL = 'https://vault.vault:8200';
const VAULT_ROLE = `${NAMESPACE}-paymentservice`;
const SECRET_PATH = `secret/data/${NAMESPACE}/stripe`;
const SERVICE_ACCOUNT_TOKEN_PATH = '/var/run/secrets/kubernetes.io/serviceaccount/token';

/**
 * Retrieves Stripe API key from HashiCorp Vault using Kubernetes authentication
 * @returns {Promise<string>} The Stripe API key
 */
async function secret(logger) {
  try {
    logger.info('Attempting to fetch secret from Vault...');
    
    // Read Kubernetes service account token
    const jwt = fs.readFileSync(SERVICE_ACCOUNT_TOKEN_PATH, 'utf8');

    // Initialize Vault client
    const vaultClient = vault({
      apiVersion: 'v1',
      endpoint: VAULT_URL,
      requestOptions: {
        // Skip TLS certificate validation
        strictSSL: false
      }
    });

    logger.info(`Authenticating with Vault using role: ${VAULT_ROLE}`);
    
    // Authenticate to Vault using Kubernetes auth method
    const authResult = await vaultClient.kubernetesLogin({
      role: VAULT_ROLE,
      jwt: jwt
    });

    // Set token for subsequent requests
    vaultClient.token = authResult.auth.client_token;

    logger.info(`Fetching secret from path: ${SECRET_PATH}`);
    
    // Fetch the secret
    const secretResult = await vaultClient.read(SECRET_PATH);
    
    // In KV-v2 secrets engine, the actual data is nested under data.data
    if (!secretResult.data || !secretResult.data.data || !secretResult.data.data.api_key) {
      throw new Error('Secret response did not contain expected api_key field');
    }
    
    logger.info('Successfully retrieved Stripe API key from Vault');
    
    // Return the API key
    return secretResult.data.data.api_key;
    
  } catch (error) {
    throw new Error(`Error retrieving secret from Vault: ${error.message}`);
  }
}

module.exports = secret;