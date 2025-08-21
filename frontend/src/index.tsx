import React from 'react';
import ReactDOM from 'react-dom/client';
import ChatInterface from './components/ChatInterface';
import './index.css';

interface SessionData {
  role: string | null;
  roleId: number | null;
  sessionRoles: string[];
}

const dataEl = document.getElementById('session-data');
const sessionData: SessionData = dataEl
  ? JSON.parse(dataEl.textContent || '{}')
  : { role: null, roleId: null, sessionRoles: [] };

(window as any).sessionData = sessionData;

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <ChatInterface />
  </React.StrictMode>,
);

