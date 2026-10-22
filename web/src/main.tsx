import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'
// 代码高亮主题（配合 rehype-highlight）
import 'highlight.js/styles/github-dark.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
