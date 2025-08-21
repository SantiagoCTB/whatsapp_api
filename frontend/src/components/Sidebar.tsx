import React, { useState } from 'react';
import { Contact } from '../types/chat';
import Avatar from './Avatar';

interface SidebarProps {
  contacts: Contact[];
  currentChat: string | null;
  onSelect: (numero: string) => void;
}

const roleStyles: Record<'usuario' | 'bot' | 'admin', string> = {
  usuario: 'bg-blue-100 text-blue-800',
  bot: 'bg-green-100 text-green-800',
  admin: 'bg-red-100 text-red-800',
};

const Sidebar: React.FC<SidebarProps> = ({ contacts, currentChat, onSelect }) => {
  const [query, setQuery] = useState('');
  const filtered = contacts.filter(c => {
    const name = c.alias || c.numero;
    return name.toLowerCase().includes(query.toLowerCase());
  });

  return (
    <aside className="flex flex-col w-64 bg-secondary text-white shadow-elegant">
      <div className="p-2">
        <input
          type="text"
          placeholder="Buscar..."
          className="w-full p-2 rounded text-black"
          value={query}
          onChange={e => setQuery(e.target.value)}
        />
      </div>
      <ul className="flex-1 overflow-y-auto">
        {filtered.map(c => (
          <li
            key={c.numero}
            className={`flex items-center gap-2 p-2 cursor-pointer hover:bg-primary/10 ${
              currentChat === c.numero ? 'bg-primary/10' : ''
            }`}
            onClick={() => onSelect(c.numero)}
          >
            <Avatar name={c.alias || c.numero} photoUrl={c.avatarUrl} />
            <div className="flex flex-col">
              <span className="text-sm font-medium">{c.alias || c.numero}</span>
              {c.alias && <span className="text-xs text-gray-200">{c.numero}</span>}
            </div>
            {c.role && (
              <span className={`badge-role ml-auto ${roleStyles[c.role]}`}>{c.role}</span>
            )}
          </li>
        ))}
      </ul>
    </aside>
  );
};

export default Sidebar;
