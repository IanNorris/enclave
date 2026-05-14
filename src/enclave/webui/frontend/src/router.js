import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/login', name: 'login', component: () => import('./views/Login.vue'), meta: { public: true } },
  { path: '/', redirect: '/sessions' },
  {
    path: '/sessions',
    name: 'sessions',
    component: () => import('./views/Sessions.vue'),
  },
  {
    path: '/sessions/:id',
    name: 'session-detail',
    component: () => import('./views/SessionDetail.vue'),
  },
  {
    path: '/bugs',
    name: 'bugs',
    component: () => import('./views/Bugs.vue'),
  },
  {
    path: '/bugs/:session/:id',
    name: 'bug-detail',
    component: () => import('./views/BugDetail.vue'),
  },
  {
    path: '/memories',
    name: 'memories',
    component: () => import('./views/Memories.vue'),
  },
  {
    path: '/chat',
    name: 'chat',
    component: () => import('./views/Chat.vue'),
  },
  {
    path: '/artifacts',
    name: 'artifacts',
    component: () => import('./views/Artifacts.vue'),
  },
  {
    path: '/asks',
    name: 'asks',
    component: () => import('./views/Asks.vue'),
  },
  {
    path: '/timeline',
    name: 'timeline',
    component: () => import('./views/Timeline.vue'),
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  const token = localStorage.getItem('enclave_token')
  if (!to.meta.public && !token) {
    return { name: 'login' }
  }
})

export default router
