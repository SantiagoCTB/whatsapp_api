export interface Contact {
  numero: string;
  alias?: string;
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
