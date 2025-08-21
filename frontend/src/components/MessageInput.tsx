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
      className="flex-1 p-1 border rounded"
    />
    <button onClick={onSendText} className="px-3 py-1 rounded bg-primary text-white shadow-elegant">
      Enviar
    </button>
  </div>
);

export default MessageInput;
