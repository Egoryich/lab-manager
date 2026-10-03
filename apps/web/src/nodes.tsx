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
          {node.storages && node.storages.length > 0 && (
            <div className="resource-estimate">
              <h3>Хранилища</h3>
              <p className="muted">
                Наблюдаемые значения Proxmox. Свободное место здесь не означает доступный лимит
                для новых машин. PV — устройства, которые LVM связывает с пулом; их проверка
                для запуска занятий ещё не завершена.
              </p>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Имя</th>
                      <th>Тип</th>
                      <th>Всего</th>
                      <th>Свободно</th>
                      <th>Thin metadata</th>
                      <th>Томов найдено</th>
                      <th>PV по LVM</th>
                      <th>Проверка PV</th>
                    </tr>
                  </thead>
                  <tbody>
                    {node.storages.map((storage) => (
                      <tr key={storage.name}>
                        <td>{storage.name}</td>
                        <td>{storage.backend}</td>
                        <td>
                          {storage.total_bytes === null ? '—' : `${gib(storage.total_bytes)} GiB`}
                        </td>
                        <td>
                          {storage.available_bytes === null
                            ? '—'
                            : `${gib(storage.available_bytes)} GiB`}
                        </td>
                        <td>
                          {storage.thin_metadata_percent === null
                            ? '—'
                            : `${storage.thin_metadata_percent}%`}
                        </td>
                        <td>{storage.observed_volume_count ?? '—'}</td>
                        <td>{storage.physical_volumes?.join(', ') || '—'}</td>
                        <td>
                          {storage.physical_backing_reconciled === null
                            ? 'Нет данных'
                            : storage.physical_backing_reconciled
                              ? 'Завершена'
                              : 'Не завершена'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          {node.network_bridges && (
            <div className="resource-estimate">
              <h3>Сетевые мосты</h3>
              <p className="muted">
                Подключения, обнаруженные Proxmox. Отсутствие физического порта не подтверждает
                изоляцию учебных машин.
              </p>
              {node.network_bridges.length === 0 ? (
                <p>Мосты не найдены.</p>
              ) : (
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Мост</th>
                        <th>Состояние</th>
                        <th>Подключённые порты</th>
                      </tr>
                    </thead>
                    <tbody>
                      {node.network_bridges.map((bridge) => (
                        <tr key={bridge.name}>
                          <td>{bridge.name}</td>
                          <td>{bridge.active ? 'Активен' : 'Неактивен'}</td>
                          <td>{bridge.ports.join(', ') || 'Нет'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
          {!node.admission_ready && (
            <p className="muted">Запуск занятий пока заблокирован проверками ресурсов и сети.</p>
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
