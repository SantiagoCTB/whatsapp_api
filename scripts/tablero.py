import streamlit as st, pandas as pd, requests
import matplotlib.pyplot as plt
from wordcloud import WordCloud

st.set_page_config(page_title="Tablero", layout="wide")

base_url = st.secrets.get("BASE_URL", "http://localhost:5000")

st.title("Tablero")

with st.sidebar:
    start = st.date_input("Desde")
    end = st.date_input("Hasta")
    limit = st.number_input("Límite de palabras", min_value=1, value=10)
    if st.button("Actualizar"):
        st.experimental_rerun()

params = {
    k: v.isoformat() if hasattr(v, "isoformat") else v
    for k, v in {"start": start, "end": end, "limit": limit}.items() if v
}


def fetch_json(endpoint: str):
    resp = requests.get(base_url + endpoint, params=params)
    resp.raise_for_status()
    return resp.json()

# Totales de mensajes
st.subheader("Totales de mensajes")
tot_df = pd.DataFrame([fetch_json("/datos_totales")])
st.bar_chart(tot_df)

# Mensajes por Día y Hora
col1, col2 = st.columns(2)
with col1:
    st.subheader("Mensajes por Día")
    diario_df = pd.DataFrame(fetch_json("/datos_mensajes_diarios")).set_index("fecha")
    st.line_chart(diario_df)
with col2:
    st.subheader("Mensajes por Hora")
    hora_df = pd.DataFrame(fetch_json("/datos_mensajes_hora"))
    horas = pd.DataFrame({"hora": list(range(24))})
    hora_df = horas.merge(hora_df, on="hora", how="left").fillna(0).set_index("hora")
    st.bar_chart(hora_df)

# Mensajes por Usuario y Top Números
col3, col4 = st.columns(2)
with col3:
    st.subheader("Mensajes por Usuario")
    tablero_df = pd.DataFrame(fetch_json("/datos_tablero")).set_index("numero")
    st.bar_chart(tablero_df)
with col4:
    st.subheader("Top Números")
    top_df = pd.DataFrame(fetch_json("/datos_top_numeros")).set_index("numero")
    st.bar_chart(top_df)

# Palabras, Roles y Tipos de Mensaje
col5, col6, col7 = st.columns(3)
with col5:
    st.subheader("Palabras Más Usadas")
    freqs = fetch_json("/datos_palabras")
    if freqs:
        wc = WordCloud(width=800, height=400, background_color="white").generate_from_frequencies(freqs)
        fig, ax = plt.subplots()
        ax.imshow(wc, interpolation="bilinear")
        ax.axis("off")
        st.pyplot(fig)
    else:
        st.write("No hay datos")
with col6:
    st.subheader("Roles")
    roles_df = pd.DataFrame(fetch_json("/datos_roles"))
    fig, ax = plt.subplots()
    ax.pie(roles_df["cantidad"], labels=roles_df["rol"], autopct="%1.1f%%")
    ax.axis("equal")
    st.pyplot(fig)
with col7:
    st.subheader("Tipos de Mensaje")
    tipos_df = pd.DataFrame(fetch_json("/datos_tipos"))
    fig, ax = plt.subplots()
    ax.pie(tipos_df["cantidad"], labels=tipos_df["tipo"], autopct="%1.1f%%", wedgeprops={"width": 0.4})
    ax.axis("equal")
    st.pyplot(fig)
