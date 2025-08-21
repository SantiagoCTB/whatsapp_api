import React from 'react';

interface MessageInputProps {
  text: string;
  onTextChange: (value: string) => void;
  onSendText: () => void;
  onSendMedia: (
    e: React.ChangeEvent<HTMLInputElement>,
    tipo: 'image' | 'audio' | 'video'
  ) => void;
}

const MessageInput: React.FC<MessageInputProps> = ({
  text,
  onTextChange,
  onSendText,
  onSendMedia,
}) => (
  <div className="flex items-center gap-2 p-2 border-t">
    <input type="file" accept="image/*" onChange={e => onSendMedia(e, 'image')} />
    <input type="file" accept="audio/*" onChange={e => onSendMedia(e, 'audio')} />
    <input type="file" accept="video/*" onChange={e => onSendMedia(e, 'video')} />
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
