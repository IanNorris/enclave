<template>
  <div class="login-page">
    <div class="login-card card">
      <h1>Enclave</h1>
      <form @submit.prevent="login">
        <div class="field">
          <label>Username</label>
          <input v-model="username" type="text" autocomplete="username" autofocus />
        </div>
        <div class="field">
          <label>Password</label>
          <input v-model="password" type="password" autocomplete="current-password" />
        </div>
        <p v-if="error" class="error">{{ error }}</p>
        <button class="primary" type="submit" :disabled="loading">
          {{ loading ? 'Signing in…' : 'Sign in' }}
        </button>
      </form>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth.js'

const router = useRouter()
const auth = useAuthStore()
const username = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)

async function login() {
  error.value = ''
  loading.value = true
  try {
    await auth.login(username.value, password.value)
    router.push('/sessions')
  } catch (e) {
    error.value = e.message || 'Invalid credentials'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: var(--bg-main);
}

.login-card {
  width: 360px;
  padding: 2rem;
}

.login-card h1 {
  text-align: center;
  margin: 0 0 1.5rem;
  font-size: 1.5rem;
}

.field {
  margin-bottom: 1rem;
}

.field label {
  display: block;
  font-size: 0.8rem;
  color: var(--text-secondary);
  margin-bottom: 0.3rem;
}

.error {
  color: var(--danger);
  font-size: 0.85rem;
  margin: 0 0 0.75rem;
}

button[type="submit"] {
  width: 100%;
  padding: 0.7rem;
  margin-top: 0.5rem;
}
</style>
