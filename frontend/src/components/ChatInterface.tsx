import React, { useEffect, useRef, useState } from 'react';

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

const MessageList: React.FC<{ messages: any[] }> = ({ messages }) => {
  const endRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  return (
    <div className="messages">
      {messages.map((m, i) => {
        const [text, tipo, mediaUrl] = m;
        const waId = m[8];
        return (
          <div key={waId ?? i} className={`bubble ${tipo}`}>
            {text && <span>{text}</span>}
            {mediaUrl && <MediaContent tipo={tipo} url={mediaUrl} />}
          </div>
        );
      })}
      <div ref={endRef} />
    </div>
  );
};

const QuickButtons: React.FC<{ buttons: QuickButton[]; onSend: (b: QuickButton) => void }> = ({ buttons, onSend }) => (
  <div className="quick-buttons">
    {buttons.map((b, i) => (
      <button key={i} onClick={() => onSend(b)}>{b.nombre || i + 1}</button>
    ))}
  </div>
);

const ChatInterface: React.FC = () => {
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [currentChat, setCurrentChat] = useState<string | null>(null);
  const [messages, setMessages] = useState<any[]>([]);
  const [text, setText] = useState('');
  const [buttons, setButtons] = useState<QuickButton[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch('/get_chat_list')
      .then(r => r.json())
      .then(data => {
        setChats(data);
        setError(null);
      })
      .catch(() => setError('Error al obtener la lista de chats'));
  }, []);

  useEffect(() => {
    if (!currentChat) return;
    fetch(`/get_chat/${currentChat}`)
      .then(r => r.json())
      .then(data => {
        setMessages(data.mensajes || []);
        setError(null);
      })
      .catch(() => setError('Error al cargar el chat'));
    fetch('/get_botones')
      .then(r => r.json())
      .then(data => {
        setButtons(data);
        setError(null);
      })
      .catch(() => setError('Error al obtener los botones'));
  }, [currentChat]);

  const refreshMessages = () => {
    if (!currentChat) return;
    fetch(`/get_chat/${currentChat}`)
      .then(r => r.json())
      .then(data => {
        setMessages(data.mensajes || []);
        setError(null);
      })
      .catch(() => setError('Error al cargar el chat'));
  };

  const sendText = () => {
    if (!currentChat || !text.trim()) return;
    fetch('/send_message', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ numero: currentChat, mensaje: text })
    })
      .then(res => {
        if (res.ok) {
          setText('');
          refreshMessages();
        } else {
          setError('Error al enviar el mensaje');
        }
      })
      .catch(() => setError('Error al enviar el mensaje'));
  };

  const sendButton = (b: QuickButton) => {
    if (!currentChat) return;
    const urls = b.media_urls && b.media_urls.length ? b.media_urls : [null];
    const requests = urls.map((url, index) =>
      fetch('/send_message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          numero: currentChat,
          mensaje: index === 0 ? b.mensaje : '',
          tipo_respuesta: b.tipo,
          opciones: url
        })
      }).catch(() => {
        throw new Error('send error');
      })
    );
    Promise.all(requests)
      .then(() => {
        refreshMessages();
      })
      .catch(() => setError('Error al enviar el mensaje'));
  };

  const sendMedia = (
    e: React.ChangeEvent<HTMLInputElement>,
    tipo: 'image' | 'audio' | 'video'
  ) => {
    if (!currentChat || !e.target.files || !e.target.files.length) return;
    const file = e.target.files[0];
    const formData = new FormData();
    formData.append('numero', currentChat);
    formData.append(tipo, file);
    fetch(`/send_${tipo}`, {
      method: 'POST',
      body: formData
    })
      .then(res => {
        if (res.ok) {
          e.target.value = '';
          refreshMessages();
        } else {
          setError('Error al enviar el archivo');
        }
      })
      .catch(() => setError('Error al enviar el archivo'));
  };

  return (
    <div className="chat-container">
      {error && <div className="error-container">{error}</div>}
      <ChatList chats={chats} current={currentChat} onSelect={setCurrentChat} />
      <div className="chat-area">
        <MessageList messages={messages} />
        <QuickButtons buttons={buttons} onSend={sendButton} />
        <div className="input-row">
          <input type="file" accept="image/*" onChange={e => sendMedia(e, 'image')} />
          <input type="file" accept="audio/*" onChange={e => sendMedia(e, 'audio')} />
          <input type="file" accept="video/*" onChange={e => sendMedia(e, 'video')} />
          <input value={text} onChange={e => setText(e.target.value)} placeholder="Mensaje" />
          <button onClick={sendText}>Enviar</button>
        </div>
      </div>
    </div>
  );
};

export default ChatInterface;

