import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import App from './App.vue'
import router from './router'
import { pinia } from './stores/pinia'
import './style.css'

document.documentElement.classList.add('dark')

createApp(App).use(pinia).use(router).use(ElementPlus).mount('#app')
