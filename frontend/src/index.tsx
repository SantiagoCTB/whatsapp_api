import React from 'react';
import ReactDOM from 'react-dom/client';
import ChatTecnimedellin from './ChatTecnimedellin';
import './index.css';

interface SessionData {
  role: string | null;
  roleId: number | null;
  sessionRoles: string[];
}

const dataEl = document.getElementById('session-data');
const sessionData: SessionData = dataEl ? JSON.parse(dataEl.textContent || '{}') : { role: null, roleId: null, sessionRoles: [] };

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <ChatTecnimedellin role={sessionData.role} roleId={sessionData.roleId} sessionRoles={sessionData.sessionRoles} />
  </React.StrictMode>
);
