import React, { useEffect, useRef, useState } from 'react';
import { Message } from '../types/chat';

const MediaContent: React.FC<{ tipo: string; url: string }> = ({ tipo, url }) => {
  const [error, setError] = useState(false);
  if (error) {
    return <div className="media-error">No se pudo cargar el archivo</div>;
  }
  if (tipo && tipo.includes('image')) {
    return <img src={url} className="media-image" onError={() => setError(true)} alt="imagen" />;
  }
  if (tipo && tipo.includes('audio')) {
    return <audio controls src={url} className="media-audio" onError={() => setError(true)} />;
  }
  if (tipo && tipo.includes('video')) {
    return <video controls src={url} className="media-video" onError={() => setError(true)} />;
  }
  return (
    <a href={url} target="_blank" rel="noopener noreferrer" className="media-link">
      {url}
    </a>
  );
};

interface MessageListProps {
  messages: Message[];
}

const MessageList: React.FC<MessageListProps> = ({ messages }) => {
  const endRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  return (
    <div className="flex-1 p-2 overflow-y-auto">
      {messages.map((m, i) => {
        const base = 'max-w-[70%] p-2 my-1 rounded-lg break-words transition-opacity duration-300 animate-fade-in';
        const isBot =
          m.tipo === 'bot' ||
          m.tipo === 'asesor' ||
          m.tipo?.startsWith('bot_') ||
          m.tipo?.startsWith('asesor_');
        const isAdmin = m.tipo === 'admin' || m.tipo?.startsWith('admin_');
        const bubbleClass = [
          base,
          isBot ? 'bg-bubble-bot self-end' : isAdmin ? 'bg-bubble-admin self-end' : 'bg-bubble-user self-start'
        ].join(' ');
        const label = isBot ? 'bot' : isAdmin ? 'admin' : 'usuario';
        return (
          <div key={m.waId ?? i} className={bubbleClass} role="status" aria-label={`Mensaje de ${label}`}>
            {m.text && <span>{m.text}</span>}
            {m.mediaUrl && <MediaContent tipo={m.tipo} url={m.mediaUrl} />}
          </div>
        );
      })}
      <div ref={endRef} />
    </div>
  );
};

export default MessageList;
