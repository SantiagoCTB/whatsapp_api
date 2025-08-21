import React, { useRef, useState } from 'react';
import Dropdown from './Dropdown';

interface QuickActionsProps {
  onSendMedia: (file: File, tipo: 'image' | 'audio' | 'video') => Promise<void>;
}

// inline SVG icons to avoid external dependencies
const PaperclipIcon = () => (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    className="h-5 w-5"
  >
    <path d="M21.44 11.05l-9.19 9.19a5.6 5.6 0 01-7.92-7.92l10.31-10.3a3.8 3.8 0 015.38 5.38L9.88 18.54a2 2 0 01-2.83-2.83l9.9-9.9" />
  </svg>
);

const ImageIcon = () => (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    className="h-5 w-5"
  >
    <rect x="3" y="3" width="18" height="18" rx="2" />
    <circle cx="8.5" cy="8.5" r="1.5" />
    <path d="M21 15l-5-5L5 21" />
  </svg>
);

const MicIcon = () => (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    className="h-5 w-5"
  >
    <rect x="9" y="2" width="6" height="11" rx="3" />
    <path d="M12 17v5M8 21h8" />
    <path d="M5 10v2a7 7 0 0014 0v-2" />
  </svg>
);

const VideoIcon = () => (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    className="h-5 w-5"
  >
    <rect x="3" y="5" width="15" height="14" rx="2" />
    <path d="M21 7l-5 4 5 4V7z" />
  </svg>
);

const LoaderIcon = () => (
  <svg
    viewBox="0 0 24 24"
    className="h-5 w-5 animate-spin"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
  >
    <circle
      cx="12"
      cy="12"
      r="10"
      strokeOpacity="0.25"
      className="text-gray-400"
    />
    <path d="M22 12a10 10 0 00-10-10" className="text-current" />
  </svg>
);

const QuickActions: React.FC<QuickActionsProps> = ({ onSendMedia }) => {
  const imageRef = useRef<HTMLInputElement>(null);
  const audioRef = useRef<HTMLInputElement>(null);
  const videoRef = useRef<HTMLInputElement>(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState('');

  const handleChange = async (
    e: React.ChangeEvent<HTMLInputElement>,
    tipo: 'image' | 'audio' | 'video'
  ) => {
    if (!e.target.files || !e.target.files.length) return;
    const file = e.target.files[0];
    setLoading(true);
    setMessage('Enviando archivo...');
    try {
      await onSendMedia(file, tipo);
      setMessage('Archivo enviado correctamente');
    } catch {
      setMessage('Error al enviar el archivo');
    } finally {
      setLoading(false);
      e.target.value = '';
    }
  };

  return (
    <div className="relative flex items-center">
      <Dropdown
        trigger={
          <div aria-describedby="attach-tip">
            <PaperclipIcon />
            <span id="attach-tip" role="tooltip" className="sr-only">
              Adjuntar archivo
            </span>
          </div>
        }
      >
        <button
          onClick={() => imageRef.current?.click()}
          className="flex items-center gap-2 px-3 py-2 hover:bg-gray-100"
          role="menuitem"
          aria-describedby="image-tip"
        >
          <ImageIcon />
          <span id="image-tip" role="tooltip" className="sr-only">
            Imagen
          </span>
        </button>
        <button
          onClick={() => audioRef.current?.click()}
          className="flex items-center gap-2 px-3 py-2 hover:bg-gray-100"
          role="menuitem"
          aria-describedby="audio-tip"
        >
          <MicIcon />
          <span id="audio-tip" role="tooltip" className="sr-only">
            Audio
          </span>
        </button>
        <button
          onClick={() => videoRef.current?.click()}
          className="flex items-center gap-2 px-3 py-2 hover:bg-gray-100"
          role="menuitem"
          aria-describedby="video-tip"
        >
          <VideoIcon />
          <span id="video-tip" role="tooltip" className="sr-only">
            Video
          </span>
        </button>
      </Dropdown>

      <input
        type="file"
        ref={imageRef}
        accept="image/*"
        onChange={e => handleChange(e, 'image')}
        className="hidden"
      />
      <input
        type="file"
        ref={audioRef}
        accept="audio/*"
        onChange={e => handleChange(e, 'audio')}
        className="hidden"
      />
      <input
        type="file"
        ref={videoRef}
        accept="video/*"
        onChange={e => handleChange(e, 'video')}
        className="hidden"
      />

      {loading && <LoaderIcon />}
      <p className="sr-only" aria-live="polite">
        {message}
      </p>
    </div>
  );
};

export default QuickActions;

