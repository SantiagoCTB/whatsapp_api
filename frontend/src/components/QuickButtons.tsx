import React from 'react';
import { QuickButton } from '../types/chat';

interface QuickButtonsProps {
  buttons: QuickButton[];
  onSend: (button: QuickButton) => void;
}

const QuickButtons: React.FC<QuickButtonsProps> = ({ buttons, onSend }) => (
  <div className="flex gap-2 p-2 border-t bg-gray-50">
    {buttons.map((b, i) => (
      <button
        key={i}
        className="px-3 py-1 border rounded bg-white shadow-elegant"
        onClick={() => onSend(b)}
      >
        {b.nombre || i + 1}
      </button>
    ))}
  </div>
);

export default QuickButtons;
