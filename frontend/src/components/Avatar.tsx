import React from 'react';

interface AvatarProps {
  name: string;
  photoUrl?: string;
}

const Avatar: React.FC<AvatarProps> = ({ name, photoUrl }) => {
  if (photoUrl) {
    return <img src={photoUrl} alt={name} className="w-10 h-10 rounded-full" />;
  }
  const initials = name
    .split(' ')
    .map((n) => n[0])
    .join('')
    .slice(0, 2)
    .toUpperCase();
  return (
    <div className="w-10 h-10 rounded-full bg-primary/20 flex items-center justify-center text-primary font-semibold">
      {initials}
    </div>
  );
};

export default Avatar;
