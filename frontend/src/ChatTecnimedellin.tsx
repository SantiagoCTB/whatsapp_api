import React, { useEffect, useState } from 'react';

interface ChatSummary {
  numero: string;
  alias?: string;
}

interface QuickButton {
  nombre?: string;
  mensaje: string;
  tipo: string;
  media_urls?: string[];
}

const ChatList: React.FC<{ chats: ChatSummary[]; current: string | null; onSelect: (n: string) => void }> = ({ chats, current, onSelect }) => (
  <aside className="chat-list">
    <ul>
      {chats.map(c => (
        <li key={c.numero} className={current === c.numero ? 'active' : ''} onClick={() => onSelect(c.numero)}>
          {c.alias ? `${c.alias} (${c.numero})` : c.numero}
        </li>
      ))}
    </ul>
  </aside>
);

const MessageList: React.FC<{ messages: any[] }> = ({ messages }) => (
  <div className="messages">
    {messages.map((m, i) => {
      const [text, tipo, mediaUrl] = m;
      return (
        <div key={i} className={`bubble ${tipo}`}>
          {text && <span>{text}</span>}
          {mediaUrl && <MediaContent tipo={tipo} url={mediaUrl} />}
        </div>
      );
    })}
  </div>
);

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

const QuickButtons: React.FC<{ buttons: QuickButton[]; onSend: (b: QuickButton) => void }> = ({ buttons, onSend }) => (
  <div className="quick-buttons">
    {buttons.map((b, i) => (
      <button key={i} onClick={() => onSend(b)}>{b.nombre || i + 1}</button>
    ))}
  </div>
);

interface ChatTecnimedellinProps {
  role: string | null;
  roleId: number | null;
  sessionRoles: string[];
}

const ChatTecnimedellin: React.FC<ChatTecnimedellinProps> = ({ role, roleId, sessionRoles }) => {
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [currentChat, setCurrentChat] = useState<string | null>(null);
  const [messages, setMessages] = useState<any[]>([]);
  const [text, setText] = useState('');
  const [buttons, setButtons] = useState<QuickButton[]>([]);

  useEffect(() => {
    fetch('/get_chat_list')
      .then(r => r.json())
      .then(setChats);
  }, []);

  useEffect(() => {
    if (!currentChat) return;
    fetch(`/get_chat/${currentChat}`)
      .then(r => r.json())
      .then(data => setMessages(data.mensajes || []));
    fetch('/get_botones')
      .then(r => r.json())
      .then(setButtons);
  }, [currentChat]);

  const sendText = () => {
    if (!currentChat || !text.trim()) return;
    fetch('/send_message', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ numero: currentChat, mensaje: text })
    }).then(res => {
      if (res.ok) {
        setText('');
        fetch(`/get_chat/${currentChat}`)
          .then(r => r.json())
          .then(data => setMessages(data.mensajes || []));
      }
    });
  };

  const sendButton = (b: QuickButton) => {
    if (!currentChat) return;
    const urls = b.media_urls && b.media_urls.length ? b.media_urls : [null];
    Promise.all(urls.map((url, index) => fetch('/send_message', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        numero: currentChat,
        mensaje: index === 0 ? b.mensaje : '',
        tipo_respuesta: b.tipo,
        opciones: url
      })
    }))).then(() => {
      fetch(`/get_chat/${currentChat}`)
        .then(r => r.json())
        .then(data => setMessages(data.mensajes || []));
    });
  };

  return (
    <div className="chat-container">
      <ChatList chats={chats} current={currentChat} onSelect={setCurrentChat} />
      <div className="chat-area">
        <MessageList messages={messages} />
        <QuickButtons buttons={buttons} onSend={sendButton} />
        <div className="input-row">
          <input value={text} onChange={e => setText(e.target.value)} placeholder="Mensaje" />
          <button onClick={sendText}>Enviar</button>
        </div>
      </div>
    </div>
  );
};

export default ChatTecnimedellin;
