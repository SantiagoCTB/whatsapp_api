import React, { useState } from 'react';
import Login from './Login';

function App() {
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState('');

  const handleLogin = async ({ username, password }) => {
    setLoading(true);
    setErrorMessage('');
    try {
      const response = await fetch('/api/login', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ username, password }),
      });

      const data = await response.json();

      if (!response.ok || data.status !== 'ok') {
        setErrorMessage(data.message || 'Error al iniciar sesión');
      } else {
        // Optional redirect after successful login
        window.location.href = '/';
      }
    } catch (err) {
      setErrorMessage('Error de conexión');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Login onSubmit={handleLogin} loading={loading} errorMessage={errorMessage} />
  );
}

export default App;
