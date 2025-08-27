import React, { useState } from 'react';

export default function Login() {
  const [showPassword, setShowPassword] = useState(false);

  const togglePassword = () => setShowPassword((prev) => !prev);
  const handleSubmit = (e) => {
    e.preventDefault();
    // TODO: conectar con el backend de autenticación
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-primary via-secondary to-primary-dark relative overflow-hidden">
      <div className="relative z-10 w-full max-w-sm px-8 py-10 bg-white/30 backdrop-blur-xl rounded-2xl shadow-xl">
        <h2 className="text-3xl font-bold text-center text-white mb-6">Iniciar sesión</h2>
        <form onSubmit={handleSubmit} className="space-y-6">
          <div className="relative">
            <input
              id="username"
              name="username"
              placeholder="Usuario"
              className="w-full pl-3 pr-3 py-2 rounded-lg bg-white/60 focus:bg-white/80 placeholder-white/60 text-gray-800 focus:outline-none focus:ring-2 focus:ring-primary"
            />
          </div>
          <div className="relative">
            <input
              type={showPassword ? 'text' : 'password'}
              id="password"
              name="password"
              placeholder="Contraseña"
              className="w-full pl-3 pr-10 py-2 rounded-lg bg-white/60 focus:bg-white/80 placeholder-white/60 text-gray-800 focus:outline-none focus:ring-2 focus:ring-primary"
            />
            <span
              className="absolute inset-y-0 right-0 pr-3 flex items-center cursor-pointer text-white/70"
              onClick={togglePassword}
            >
              {showPassword ? '🙈' : '👁️'}
            </span>
          </div>
          <button
            type="submit"
            className="w-full py-2 rounded-lg bg-primary text-white font-semibold hover:bg-primary-dark transition transform active:scale-95"
          >
            Ingresar
          </button>
        </form>
      </div>
    </div>
  );
}
