import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import './style.css'
import './trace.css'
import './supplements.css'
import './samples.css'

createRoot(document.getElementById('root')).render(
  <React.StrictMode><App /></React.StrictMode>
)
