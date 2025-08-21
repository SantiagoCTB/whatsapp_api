import React from 'react';
import QuickActions from './QuickActions';

interface MessageInputProps {
  text: string;
  onTextChange: (value: string) => void;
  onSendText: () => void;
  onSendMedia: (file: File, tipo: 'image' | 'audio' | 'video') => Promise<void>;
}

const MessageInput: React.FC<MessageInputProps> = ({
  text,
  onTextChange,
  onSendText,
  onSendMedia,
}) => (
  <div className="flex items-center gap-2 p-2 border-t">
    <QuickActions onSendMedia={onSendMedia} />
    <input
      value={text}
      onChange={e => onTextChange(e.target.value)}
      placeholder="Mensaje"
      aria-label="Escribir mensaje"
      className="flex-1 p-1 border rounded focus:outline-none focus:ring-2 focus:ring-primary"
    />
    <button
      onClick={onSendText}
      aria-label="Enviar mensaje"
      className="px-3 py-1 rounded bg-primary text-white shadow-elegant focus:outline-none focus:ring-2 focus:ring-primary"
    >
      Enviar
    </button>
  </div>
);

export default MessageInput;
