import React, { useEffect, useState } from 'react';
import Sidebar from './Sidebar';
import MessageList from './MessageList';
import MessageInput from './MessageInput';
import QuickButtons from './QuickButtons';
import { Contact, Message, QuickButton } from '../types/chat';

const ChatInterface: React.FC = () => {
  const [chats, setChats] = useState<Contact[]>([]);
  const [currentChat, setCurrentChat] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [text, setText] = useState('');
  const [buttons, setButtons] = useState<QuickButton[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch('/get_chat_list')
      .then(r => r.json())
      .then((data: Contact[]) => {
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
        const mapped: Message[] = (data.mensajes || []).map((m: any[]) => ({
          text: m[0],
          tipo: m[1],
          mediaUrl: m[2],
          waId: m[8],
        }));
        setMessages(mapped);
        setError(null);
      })
      .catch(() => setError('Error al cargar el chat'));
    fetch('/get_botones')
      .then(r => r.json())
      .then((data: QuickButton[]) => {
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
        const mapped: Message[] = (data.mensajes || []).map((m: any[]) => ({
          text: m[0],
          tipo: m[1],
          mediaUrl: m[2],
          waId: m[8],
        }));
        setMessages(mapped);
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

  const sendMedia = async (file: File, tipo: 'image' | 'audio' | 'video') => {
    if (!currentChat) throw new Error('No chat seleccionado');
    const formData = new FormData();
    formData.append('numero', currentChat);
    formData.append(tipo, file);
    const res = await fetch(`/send_${tipo}`, {
      method: 'POST',
      body: formData,
    });
    if (!res.ok) {
      setError('Error al enviar el archivo');
      throw new Error('upload failed');
    }
    refreshMessages();
  };

  return (
    <div className="flex h-screen bg-gradient-primary">
      {error && <div className="error-container">{error}</div>}
      <Sidebar contacts={chats} currentChat={currentChat} onSelect={setCurrentChat} />
      <div className="flex flex-1 flex-col bg-white">
        <MessageList messages={messages} />
        <QuickButtons buttons={buttons} onSend={sendButton} />
        <MessageInput
          text={text}
          onTextChange={setText}
          onSendText={sendText}
          onSendMedia={sendMedia}
        />
      </div>
    </div>
  );
};

export default ChatInterface;
