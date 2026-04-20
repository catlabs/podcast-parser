import { useEffect, useState } from "react";
import { getEpisodes, type CollectionTag, type Episode } from "../api";

function CollectionBadges({ collections }: { collections: CollectionTag[] | undefined }) {
  if (!collections?.length) return <span className="muted">—</span>;
  return (
    <span className="collection-badges">
      {collections.map(c => (
        <span key={c.key} className="collection-badge" title={c.key}>
          {c.label}
        </span>
      ))}
    </span>
  );
}

export default function EpisodeList() {
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [loading, setLoading]   = useState(true);
  const [error, setError]       = useState<string | null>(null);

  useEffect(() => {
    getEpisodes()
      .then(setEpisodes)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <p className="muted">Loading episodes…</p>;
  if (error)   return <p className="error">Error: {error}</p>;
  if (!episodes.length) return <p className="muted">No episodes indexed yet. Run ingest first.</p>;

  return (
    <div>
      <h2>Indexed episodes <span className="badge">{episodes.length}</span></h2>
      <div className="table-scroll">
        <table className="episodes-table">
          <colgroup>
            <col style={{ width: "100px" }} />
            <col />
            <col style={{ width: "80px" }} />
            <col style={{ width: "220px" }} />
          </colgroup>
          <thead>
            <tr>
              <th>Date</th>
              <th>Title</th>
              <th className="center">Chunks</th>
              <th>Collections</th>
            </tr>
          </thead>
          <tbody>
            {episodes.map((ep) => (
              <tr key={ep.id}>
                <td className="muted nowrap">{ep.date ?? "—"}</td>
                <td>{ep.title}</td>
                <td className="center">
                  <span className="badge">{ep.chunk_count}</span>
                </td>
                <td>
                  <CollectionBadges collections={ep.collections} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
