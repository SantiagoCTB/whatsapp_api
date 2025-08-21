import React from 'react';
import { Contact } from '../types/chat';

interface SidebarProps {
  contacts: Contact[];
  currentChat: string | null;
  onSelect: (numero: string) => void;
}

const Sidebar: React.FC<SidebarProps> = ({ contacts, currentChat, onSelect }) => (
  <aside className="flex flex-col w-64 bg-secondary shadow-elegant overflow-y-auto">
    <ul>
      {contacts.map(c => (
        <li
          key={c.numero}
          className={`p-2 cursor-pointer ${currentChat === c.numero ? 'bg-primary text-white' : ''}`}
          onClick={() => onSelect(c.numero)}
        >
          {c.alias ? `${c.alias} (${c.numero})` : c.numero}
        </li>
      ))}
    </ul>
  </aside>
);

export default Sidebar;
