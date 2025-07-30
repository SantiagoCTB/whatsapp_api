// script.js

let currentChat = null;
let autoRefreshInterval = null;
let todosLosChats = [];
let lastMsgCount = 0;

// Carga nuevos mensajes y los añade con animación, sin parpadear ni reiniciar el scroll
function fetchChat() {
  if (!currentChat) return;
  // Si hay audio o video reproduciéndose, no recargamos
  const medias = document.querySelectorAll('#chatBox audio, #chatBox video');
  for (const m of medias) {
    if (!m.paused) return;
  }

  fetch(`/get_chat/${currentChat}`)
    .then(res => res.json())
    .then(data => {
      const chatBox = document.getElementById('chatBox');
      const msgs    = data.mensajes;
      const total   = msgs.length;
      const atBottom = chatBox.scrollHeight - chatBox.scrollTop <= chatBox.clientHeight + 5;

      // Si primera carga o el servidor devolvió menos (reinicio), limpiamos
      if (lastMsgCount === 0 || total < lastMsgCount) {
        chatBox.innerHTML = '';
        lastMsgCount = 0;
      }

      // Añadimos solo los mensajes nuevos
      for (let i = lastMsgCount; i < total; i++) {
        const [texto, tipo, media_url, ts] = msgs[i];
        const cont = document.createElement('div');
        cont.className = `${tipo} message`;

        if (tipo.endsWith('_image') && media_url) {
          const img = document.createElement('img');
          img.src = media_url;
          img.style.maxWidth = '200px';
          cont.appendChild(img);
          if (texto) {
            const p = document.createElement('p');
            p.textContent = texto;
            cont.appendChild(p);
          }
        }
        else if (tipo.includes('audio') && media_url) {
          const audio = document.createElement('audio');
          audio.controls = true;
          audio.src = media_url;
          cont.appendChild(audio);
          if (texto) {
            const p = document.createElement('p');
            p.textContent = texto;
            cont.appendChild(p);
          }
        }
        else if (tipo.includes('video') && media_url) {
          const vid = document.createElement('video');
          vid.controls = true;
          vid.src = media_url;
          vid.style.maxWidth = '200px';
          cont.appendChild(vid);
          if (texto) {
            const p = document.createElement('p');
            p.textContent = texto;
            cont.appendChild(p);
          }
        }
        else {
          cont.textContent = `[${ts}] ${tipo}: ${texto}`;
        }

        chatBox.appendChild(cont);
      }

      lastMsgCount = total;
      if (atBottom) chatBox.scrollTop = chatBox.scrollHeight;
    })
    .catch(err => console.error('Error al obtener chat:', err));
}

// Obtiene la lista de chats y la pinta
function fetchChatList() {
  fetch('/get_chat_list')
    .then(res => res.json())
    .then(data => {
      todosLosChats = data;
      renderChatList(data);
    })
    .catch(err => console.error('Error al obtener lista de chats:', err));
}

// Pinta la lista de clientes en la barra lateral
function renderChatList(lista) {
  const chatList = document.getElementById('chatList');
  chatList.innerHTML = '';
  lista.forEach(chat => {
    const li = document.createElement('li');
    li.textContent = chat.alias
      ? `${chat.alias} (${chat.numero})`
      : chat.numero;
    li.className = chat.asesor ? 'asesor-activo' : '';
    li.onclick = () => loadChat(chat.numero);
    li.addEventListener('contextmenu', e => {
      e.preventDefault();
      mostrarMenuContextual(e.pageX, e.pageY, chat);
    });
    chatList.appendChild(li);
  });
}

// Selecciona un chat y comienza el refresco
function loadChat(numero) {
  currentChat = numero;
  lastMsgCount = 0;
  fetchChat();
  startAutoRefresh();
}

// Inicia el auto‐refresh cada 3s
function startAutoRefresh() {
  if (autoRefreshInterval) clearInterval(autoRefreshInterval);
  autoRefreshInterval = setInterval(() => {
    fetchChatList();
    fetchChat();
  }, 3000);
}

// Envía un mensaje de texto
function sendMessage() {
  const input = document.getElementById('messageInput');
  const mensaje = input.value.trim();
  if (!mensaje || !currentChat) return alert("Selecciona un chat");
  fetch('/send_message', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ numero: currentChat, mensaje })
  })
    .then(() => {
      input.value = '';
      fetchChat();
      fetchChatList();
    })
    .catch(err => console.error('Error al enviar mensaje:', err));
}

// -------------------- Adjuntar (Attach) --------------------

const attachBtn  = document.getElementById('attachBtn');
const attachMenu = document.getElementById('attachMenu');
attachBtn.addEventListener('click', () => {
  attachMenu.classList.toggle('show');
});
// Cerrar si clic fuera
document.addEventListener('click', e => {
  if (!attachBtn.contains(e.target) && !attachMenu.contains(e.target)) {
    attachMenu.classList.remove('show');
  }
});

// Inputs ocultos
const imageInput = document.getElementById('imageInput');
const audioInput = document.getElementById('audioInput');
const videoInput = document.getElementById('videoInput');

// Botones del menú
document.getElementById('attachImage')
  .addEventListener('click', () => imageInput.click());
document.getElementById('attachAudio')
  .addEventListener('click', () => audioInput.click());
document.getElementById('attachVideo')
  .addEventListener('click', () => videoInput.click());

// Envío de imagen
imageInput.addEventListener('change', async () => {
  if (!currentChat) return alert("Selecciona un chat");
  const file = imageInput.files[0];
  if (!file) return;
  const form = new FormData();
  form.append('image', file);
  form.append('caption','');
  form.append('numero', currentChat);
  try {
    const resp = await fetch('/send_image', { method:'POST', body:form });
    if (!resp.ok) throw new Error(await resp.text());
    imageInput.value = '';
    attachMenu.classList.remove('show');
    fetchChat();
    fetchChatList();
  } catch(err) {
    console.error('Error enviando imagen:', err);
    alert('Error enviando imagen');
  }
});

// Envío de audio
audioInput.addEventListener('change', async () => {
  if (!currentChat) return alert("Selecciona un chat");
  const file = audioInput.files[0];
  if (!file) return;
  const form = new FormData();
  form.append('audio', file);
  form.append('caption','');
  form.append('numero', currentChat);
  try {
    const resp = await fetch('/send_audio', { method:'POST', body:form });
    if (!resp.ok) throw new Error(await resp.text());
    audioInput.value = '';
    attachMenu.classList.remove('show');
    fetchChat();
    fetchChatList();
  } catch(err) {
    console.error('Error enviando audio:', err);
    alert('Error enviando audio');
  }
});

// Envío de video
videoInput.addEventListener('change', async () => {
  if (!currentChat) return alert("Selecciona un chat");
  const file = videoInput.files[0];
  if (!file) return;
  const form = new FormData();
  form.append('video', file);
  form.append('caption','');
  form.append('numero', currentChat);
  try {
    const resp = await fetch('/send_video', { method:'POST', body:form });
    if (!resp.ok) throw new Error(await resp.text());
    videoInput.value = '';
    attachMenu.classList.remove('show');
    fetchChat();
    fetchChatList();
  } catch(err) {
    console.error('Error enviando video:', err);
    alert('Error enviando video');
  }
});

// Obtiene y pinta los botones rápidos
function fetchBotones() {
  fetch('/get_botones')
    .then(res => res.json())
    .then(data => {
      const cont = document.getElementById('botonera');
      cont.innerHTML = '';
      data.forEach((b, i) => {
        const btn = document.createElement('button');
        btn.textContent = i + 1;
        btn.title       = b.mensaje;
        btn.className   = 'ripple-button';
        btn.onclick     = () => {
          if (!currentChat) return alert("Selecciona un chat");
          fetch('/send_message', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ numero: currentChat, mensaje: b.mensaje })
          }).then(() => {
            fetchChat();
            fetchChatList();
          });
        };
        cont.appendChild(btn);
      });
    })
    .catch(err => console.error('Error al obtener botones:', err));
}

// Mostrar menú contextual para alias
let chatContextual = null;
function mostrarMenuContextual(x, y, chat) {
  chatContextual = chat;
  const menu = document.getElementById('contextMenu');
  menu.style.top     = `${y}px`;
  menu.style.left    = `${x}px`;
  menu.style.display = 'block';
}

document.addEventListener('click', () => {
  document.getElementById('contextMenu').style.display = 'none';
});

document.getElementById('menu_alias')
  .addEventListener('click', () => {
    if (!chatContextual) return;
    const nombre = prompt(
      `¿Qué nombre deseas asignar a ${chatContextual.numero}?`,
      chatContextual.alias || ''
    );
    if (nombre !== null) {
      fetch('/set_alias', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          numero: chatContextual.numero,
          nombre
        })
      }).then(() => fetchChatList());
    }
  });

// Inicialización al cargar la página
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('messageInput')
    .addEventListener('keypress', ev => {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        sendMessage();
      }
    });
  document.getElementById('buscador')
    .addEventListener('input', function() {
      const val = this.value.toLowerCase();
      renderChatList(
        todosLosChats.filter(c =>
          c.numero.toLowerCase().includes(val) ||
          (c.alias || '').toLowerCase().includes(val)
        )
      );
    });
  fetchChatList();
  fetchBotones();
});
