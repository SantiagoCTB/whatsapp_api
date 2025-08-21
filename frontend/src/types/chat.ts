export interface Contact {
  numero: string;
  alias?: string;
  role?: 'usuario' | 'bot' | 'admin';
  avatarUrl?: string;
}

export interface Message {
  text: string;
  tipo: string;
  mediaUrl?: string;
  waId?: string;
}

export interface QuickButton {
  nombre?: string;
  mensaje: string;
  tipo: string;
  media_urls?: string[];
}
