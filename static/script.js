<script>
    let currentChat = null;
    let autoRefreshInterval = null;

    function fetchChat() {
        if (!currentChat) return;
        fetch(`/get_chat/${currentChat}`)
            .then(res => res.json())
            .then(data => {
                const chatBox = document.getElementById('chatBox');
                chatBox.innerHTML = '';
                data.mensajes.forEach(msg => {
                    const div = document.createElement('div');
                    div.className = msg[1];
                    div.textContent = `[${msg[2]}] ${msg[1]}: ${msg[0]}`;
                    chatBox.appendChild(div);
                });
                chatBox.scrollTop = chatBox.scrollHeight;
            });
    }

    function fetchChatList() {
        fetch('/get_chat_list')
            .then(res => res.json())
            .then(data => {
                const chatList = document.getElementById('chatList');
                chatList.innerHTML = '';
                data.forEach(chat => {
                    const li = document.createElement('li');
                    li.textContent = chat.numero;
                    li.className = chat.asesor ? 'asesor-activo' : '';
                    li.onclick = () => loadChat(chat.numero);
                    chatList.appendChild(li);
                });
            });
    }

    function loadChat(numero) {
        currentChat = numero;
        fetchChat();
        startAutoRefresh();
    }

    function startAutoRefresh() {
        if (autoRefreshInterval) clearInterval(autoRefreshInterval);
        autoRefreshInterval = setInterval(() => {
            fetchChatList();
            fetchChat();
        }, 3000);
    }

    function sendMessage() {
        const input = document.getElementById('messageInput');
        const mensaje = input.value;
        if (!mensaje || !currentChat) return alert("Selecciona un chat");
        fetch('/send_message', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({numero: currentChat, mensaje})
        }).then(() => {
            input.value = '';
            fetchChat();
            fetchChatList();
        });
    }

    // Detectar Enter para enviar
    document.addEventListener('DOMContentLoaded', () => {
        const input = document.getElementById('messageInput');
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                sendMessage();
            }
        });
    });

    // Cargar lista inicial
    fetchChatList();
</script>
