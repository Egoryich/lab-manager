import { useQuery } from '@tanstack/react-query';
import { api, unwrap } from '@lab/client-sdk';

const gib = (value: number) => (value / 2 ** 30).toFixed(1);

export function NodePanel() {
  const query = useQuery({
    queryKey: ['nodes'],
    queryFn: async () => unwrap(await api.GET('/api/admin/nodes')),
    refetchInterval: 15_000,
  });
  return (
    <section>
      <h1>Серверы лаборатории</h1>
      <p className="muted">
        Последние сведения с физических серверов. Доступность для запуска занятий пока не
        рассчитывается.
      </p>
      {query.isPending && <p role="status">Загрузка…</p>}
      {query.error && (
        <p className="error" role="alert">
          Не удалось обновить сведения. Показанные ранее данные могли устареть.
        </p>
      )}
      {query.data?.length === 0 && <p>Серверы ещё не подключены.</p>}
      {query.data?.map((node) => (
        <article className="group-card" key={node.id}>
          <h2>{node.name}</h2>
          <p>
            <strong>
              {query.error
                ? 'Нет актуальных данных'
                : node.status === 'FRESH'
                  ? 'Данные актуальны'
                  : node.status === 'STALE'
                    ? 'Данные устарели'
                    : 'Данные пока не получены'}
            </strong>
          </p>
          <p>Снимок: {node.sampled_at ? new Date(node.sampled_at).toLocaleString('ru-RU') : '—'}</p>
          {node.host && (
            <dl>
              <dt>Логические CPU</dt>
              <dd>{node.host.logical_cpus}</dd>
              <dt>Память сервера</dt>
              <dd>{gib(node.host.memory_total_bytes)} GiB</dd>
              <dt>Свободная память в снимке</dt>
              <dd>{gib(node.host.memory_free_bytes)} GiB</dd>
              <dt>Хранилища / машины</dt>
              <dd>
                {node.storage_count} / {node.guest_count}
              </dd>
            </dl>
          )}
          {node.status !== 'FRESH' && (
            <p className="muted">
              Нет подтверждения текущего состояния. Это не означает, что сервер выключен.
            </p>
          )}
        </article>
      ))}
    </section>
  );
}
