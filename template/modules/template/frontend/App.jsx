import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';

const TemplateApp = () => {
    const [message, setMessage] = useState(null);

    useEffect(() => {
        fetch('/api/template/hello')
            .then(res => res.json())
            .then(data => setMessage(data.message))
            .catch(err => console.error('Failed to load /api/template/hello:', err));
    }, []);

    return (
        <div style={{ padding: '24px' }}>
            <h1>Template module</h1>
            <p>{message ?? 'Loading...'}</p>
        </div>
    );
};

export default TemplateApp;

const rootElement = document.getElementById('root');
if (rootElement) {
    const root = createRoot(rootElement);
    root.render(<TemplateApp />);
}
