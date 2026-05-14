/**
 * Auth helper for Playwright tests.
 * Creates a test user and obtains a JWT token via the login API.
 */

const TEST_USER = 'playwright';
const TEST_PASS = 'playwright-test-2026';

/**
 * Ensure the test user exists (idempotent).
 * Calls the Python auth module directly via a subprocess.
 */
async function ensureTestUser() {
  const { execSync } = await import('child_process');
  try {
    execSync(
      `/home/ian/.local/share/enclave/venv/bin/python3 -c "from enclave.webui.auth import create_user; create_user('${TEST_USER}', '${TEST_PASS}')"`,
      {
        env: {
          ...process.env,
          PYTHONPATH: '/home/ian/Projects/enclave/src',
        },
        stdio: 'pipe',
      }
    );
  } catch (e) {
    // User may already exist — that's fine
  }
}

/**
 * Login and inject the JWT token into localStorage so the app is authenticated.
 * Call this in a test's beforeEach or as a fixture.
 */
async function authenticate(page) {
  // Hit the login API
  const resp = await page.request.post('/api/auth/login', {
    form: { username: TEST_USER, password: TEST_PASS },
  });
  if (!resp.ok()) {
    throw new Error(`Login failed: ${resp.status()} ${await resp.text()}`);
  }
  const data = await resp.json();
  const token = data.access_token;

  // Set the token in localStorage before navigating
  await page.addInitScript((t) => {
    localStorage.setItem('enclave_token', t);
  }, token);

  return token;
}

module.exports = { ensureTestUser, authenticate, TEST_USER, TEST_PASS };
