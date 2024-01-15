from pathlib import Path
import os
import shutil

import numpy as np
from spinesUtils.asserts import raise_if
from spinesUtils.utils import Logger

from min_vec.engine import to_normalize
from min_vec.filter import BloomTrie
from min_vec.utils import io_checker, silhouette_score
from min_vec.ivf_index import CompactIVFIndex
from min_vec.kmeans import KMeans

logger = Logger(with_time=False)


class BinaryMatrixSerializer:
    def __init__(self, dim, database_path, n_clusters=8, chunk_size=100_000, dtypes=np.float32,
                 bloom_filter_size=100_000_000, device='auto') -> None:
        """
        Initialize the vector database.

        Parameters:
            dim (int): Dimension of the vectors.
            database_path (str): Path to the database file.
            chunk_size (int): The size of each data chunk. Default is 100_000.
            dtypes (str): Data type of the vectors.
                Default is 'float32'. Options are 'float32', 'float64', 'float16', 'int32', 'int64', 'int16', 'int8'.
            bloom_filter_size (int): The size of the bloom filter. Default is 100_000_000.

        Raises:
            ValueError: If `chunk_size` is less than or equal to 1.
        """
        if chunk_size <= 1:
            raise ValueError('chunk_size must be greater than 1')

        self.database_path_parent = Path(database_path).parent.absolute() / Path(
            '.mvdb'.join(Path(database_path).absolute().name.split('.mvdb')[:-1]))
        self.database_name_prefix = '.mvdb'.join(Path(database_path).name.split('.mvdb')[:-1])

        self.n_clusters = n_clusters
        self.ann_model = KMeans(n_clusters=self.n_clusters, random_state=0,
                                batch_size=10240, device=device)

        if Path(self.database_path_parent / 'ann_model.mvdb').exists():
            self.ann_model = self.ann_model.load(self.database_path_parent / 'ann_model.mvdb')

        self.device = device

        if Path(self.database_path_parent / 'ivf_index.mvdb').exists():
            self.ivf_index = (CompactIVFIndex(n_clusters=self.n_clusters).
                              load(self.database_path_parent / 'ivf_index.mvdb'))
        else:
            self.ivf_index = CompactIVFIndex(n_clusters=self.n_clusters)

        self.bloom_filter_size = bloom_filter_size
        self.dim = dim
        self.dtypes = dtypes

        # database shape
        self.database_shape = (0, self.dim)

        self.chunk_size = chunk_size

        # cluster meta data
        self.current_cluster_data_msg = dict()

        if not self.database_path_parent.exists():
            self.database_path_parent.mkdir(parents=True)

        # ============== Loading or create one empty database ==============
        # first of all, initialize a database
        self.database = []
        self.indices = []
        self.fields = []

        # If they exist, iterate through all .mvdb files.
        self.database_cluster_path = []
        self.database_chunk_path = []

        self._bloom_filter_path = self.database_path_parent / 'id_filter'

        for i in os.listdir(self.database_path_parent):
            # If it meets the naming convention, add it to the chunk list.
            # 仅添加名称中包含chunk的文件
            if (i.startswith(self.database_name_prefix) and Path(i).name.split('.')[0].split('_')[-1].isdigit()
                    and 'chunk' in i):
                self._append_to_list_without_conflict(self.database_path_parent / i, self.database_chunk_path)
            # 仅添加名称中包含cluster的文件
            if (i.startswith(self.database_name_prefix) and Path(i).name.split('.')[0].split('_')[-1].isdigit()
                    and 'cluster' in i):
                self._append_to_list_without_conflict(self.database_path_parent / i, self.database_cluster_path)

        # If the database is not empty, load the database and index.
        self._initialize_id_filter_and_shape()

        self._COMMIT_FLAG = True

    def _initialize_id_filter_and_shape(self):
        self.id_filter = BloomTrie(bloom_size=self.bloom_filter_size, bloom_hash_count=5)

        bloom_filter_initialed = False

        if self._bloom_filter_path.exists():
            self.id_filter.from_file(self._bloom_filter_path)
            bloom_filter_initialed = True

        # 初始化database形状
        if len(self.database_cluster_path) == 0:
            self.database_shape = (0, self.dim)
        else:
            length = 0
            for database, index, field in self.data_loader(self.database_cluster_path):
                length += database.shape[0]
                if not bloom_filter_initialed:
                    for idx in index:
                        self.id_filter.add(idx)
            self.database_shape = (length, self.dim)

        self.last_id = self.id_filter.find_max_value()

        if not bloom_filter_initialed:
            # save id filter
            self.id_filter.to_file(self._bloom_filter_path)

    def check_commit(self):
        if not self._COMMIT_FLAG:
            raise ValueError("Did you forget to run `commit()` function ? Try to run `commit()` first.")

    def reset_database(self):
        """Reset the database to its initial state with zeros."""
        self.database = []
        self.indices = []
        self.fields = []

    @io_checker
    def data_loader(self, paths=None):
        """
        Generator for loading the database and index.

        Parameters:
            paths (list): List of paths to load from.

        Yields:
            tuple: A tuple of (database, index, field).

        Raises:
            FileNotFoundError: If the file does not exist.
            IOError: If the file cannot be read.
            PermissionError: If the file cannot be read due to permission issues.
            UnKnownError: If an unknown error occurs.
        """
        # By default, save the checkpoint first.
        paths = paths if paths is not None else self.database_cluster_path

        for i in paths:
            with open(i, 'rb') as f:
                database = np.load(f)
                index = np.load(f, allow_pickle=True)
                field = np.load(f, allow_pickle=True)

            yield database, index, field

    @staticmethod
    def _append_to_list_without_conflict(path, plist):
        """
        Add a new chunk's path to the list of database chunks.

        Parameters:
            path (Path): Path of the new chunk.
        """
        path = str(path)
        if path not in plist:
            plist.append(path)

    def _is_database_reset(self):
        """
        Check if the database is in its reset state (filled with zeros).

        Returns:
            bool: True if the database is reset, False otherwise.
        """
        return self.database == [] and self.indices == [] and self.fields == []

    def _length_checker(self):
        """
        Check if the length of the database and index are equal.

        Raises:
            ValueError: If the lengths of the database, indices, and fields are not the same.
        """
        if not self._is_database_reset() and not self._COMMIT_FLAG:
            if not (len(self.database) == len(self.indices) == len(self.fields)):
                raise ValueError('The database, index length and field length not the same.')

    def save_and_merge_data(self, prefix, new_data, new_indices, new_fields, check_path_list):
        if len(check_path_list) == 0:
            max_id = 0
        else:
            try:
                max_id = max([int(Path(i).name.split('.')[0].split('_')[-1]) for i in check_path_list
                              if prefix in Path(i).name and Path(i).name.split('.')[0].split('_')[-1].isdigit()])

            except ValueError:
                max_id = 0

        merged_data = []
        merged_indices = []
        merged_fields = []

        # 初始化文件路径
        file_path = self.database_path_parent / f'{self.database_name_prefix}_{prefix}_{max_id}.mvdb'

        # 读取最后一个文件的数据
        if Path(file_path).exists():
            with open(file_path, 'rb') as f:
                old_data = np.load(f)
                old_indices = np.load(f, allow_pickle=True).tolist()
                old_fields = np.load(f, allow_pickle=True).tolist()

            # 如果最后一个文件的数据量大于等于chunk_size，则新建一个文件
            if old_data.shape[0] >= self.chunk_size:
                max_id += 1

                merged_data = np.vstack(new_data)
                merged_indices = new_indices
                merged_fields = new_fields
            else:
                merged_data = np.vstack((old_data, np.vstack(new_data)))

                merged_indices.extend(old_indices)
                merged_indices.extend(new_indices)
                merged_fields.extend(old_fields)
                merged_fields.extend(new_fields)

            del old_data, old_indices, old_fields
        else:
            # 如果最后一个文件不存在，则直接将新数据写入
            merged_data = np.vstack(new_data)
            merged_indices = new_indices
            merged_fields = new_fields

        def save_data():
            nonlocal merged_data, merged_indices, merged_fields, max_id, file_path, saved_paths
            # 保存数据
            with open(file_path, 'wb') as file:
                np.save(file, merged_data[:self.chunk_size, :])
                np.save(file, merged_indices[:self.chunk_size], allow_pickle=True)
                np.save(file, merged_fields[:self.chunk_size], allow_pickle=True)

            # 更新数据
            merged_data = merged_data[self.chunk_size:, :]
            merged_indices = merged_indices[self.chunk_size:]
            merged_fields = merged_fields[self.chunk_size:]

            # 更新文件路径
            saved_paths.append(file_path)

            # 更新max_id
            if merged_data.shape[0] > 0:
                max_id += 1
                file_path = self.database_path_parent / f'{self.database_name_prefix}_{prefix}_{max_id}.mvdb'

        # 更新文件路径
        file_path = self.database_path_parent / f'{self.database_name_prefix}_{prefix}_{max_id}.mvdb'
        saved_paths = []
        merged_data = np.asarray(merged_data)
        while merged_data.shape[0] > 0:
            save_data()

        return saved_paths

    @io_checker
    def save_chunk_immediately(self):
        """
        Save the current state of the database to a .mvdb file.

        Returns:
            Path: The path of the saved database file.
        """
        self._length_checker()

        if self._is_database_reset():
            return []

        paths = self.save_and_merge_data(
            'chunk',
            self.database,
            self.indices,
            self.fields,
            self.database_chunk_path
        )

        # update database shape
        old_length = self.database_shape[0]
        self.database_shape = (len(self.database) + old_length, self.dim)

        self.reset_database()  # reset database, indices and fields

        if paths:
            for fp in paths:
                self._append_to_list_without_conflict(fp, self.database_chunk_path)

        return paths

    def auto_save_chunk(self):
        self._length_checker()
        paths = []
        if len(self.database) >= self.chunk_size:
            paths = self.save_chunk_immediately()

        return paths

    def commit(self):
        """
        Save the database, ensuring that all data is written to disk.
        This method is required to be called after saving vectors to query them.
        """
        # If this method is called, the part that meets the chunk size will be saved first,
        #  and the part that does not meet the chunk size will be directly saved as the last chunk.
        if not self._COMMIT_FLAG:
            self.save_chunk_immediately()

            # save bloom filter
            self.id_filter.to_file(self._bloom_filter_path)

            self.build_index()

            # save ivf index and k-means model
            self.ann_model.save(self.database_path_parent / 'ann_model.mvdb')

            self.ivf_index.save(self.database_path_parent / 'ivf_index.mvdb')

            # del .mvdb files
            for i in self.database_chunk_path:
                os.remove(i)

            self.database_chunk_path = []

            length = 0
            for database, *_ in self.data_loader(self.database_cluster_path):
                length += database.shape[0]
            self.database_shape = (length, self.dim)

            self._COMMIT_FLAG = True

    def _generate_new_id(self):
        """
        Generate a new ID for the vector.
        """
        while True:
            self.last_id += 1
            if self.last_id not in self.id_filter:
                break

        return self.last_id

    def _process_vector_item(self, vector, index, field, normalize):
        if index in self.id_filter:
            raise ValueError(f'id {index} already exists')

        if len(vector) != self.dim:
            raise ValueError(f'vector dim error, expect {self.dim}, got {len(vector)}')

        vector = vector.astype(self.dtypes)

        if vector.ndim == 1:
            vector = vector.reshape(1, -1)

        if normalize:
            vector = to_normalize(vector)

        return vector, index, field if field is not None else ''

    def bulk_add_items(self, vectors, normalize: bool = False, save_immediately=False):
        """
        Bulk add vectors to the database in batches.

        Parameters: vectors (list or tuple): A list or tuple of vectors to be saved. Each vector can be a tuple of (
            vector, id, field).
        normalize (bool): Whether to normalize the input vector.
        save_immediately (bool): Whether to save the database immediately.

        Returns:
            list: A list of indices where the vectors are stored.
        """
        raise_if(ValueError, not isinstance(vectors, (tuple, list)),
                 f'vectors must be tuple or list, got {type(vectors)}')
        raise_if(ValueError, not isinstance(normalize, bool),
                 f'normalize must be bool, got {type(normalize)}')
        raise_if(ValueError, not isinstance(save_immediately, bool),
                 f'save_immediately must be bool, got {type(save_immediately)}')
        raise_if(ValueError, not all(1 <= len(i) <= 3 and isinstance(i, tuple) for i in vectors),
                 f'vectors must be tuple of (vector, id, field), got {vectors}')

        batch_size = 10000
        new_ids = []

        for i in range(0, len(vectors), batch_size):
            batch = vectors[i:i + batch_size]
            temp_vectors, temp_indices, temp_fields = [], [], []

            for sample in batch:
                if len(sample) == 1:
                    vector = sample[0]
                    index = None
                    field = ''
                elif len(sample) == 2:
                    vector, index = sample
                    field = ''
                else:
                    vector, index, field = sample

                index = self._generate_new_id() if index is None else index
                field = '' if field is None else field
                vector, index, field = self._process_vector_item(vector, index, field, normalize)

                temp_vectors.append(vector)
                temp_indices.append(index if index is not None else self._generate_new_id())
                temp_fields.append(field)
                self.id_filter.add(index)

            self.database.extend(temp_vectors)
            self.indices.extend(temp_indices)
            self.fields.extend(temp_fields)
            new_ids.extend(temp_indices)

            self.save_chunk_immediately() if save_immediately else self.auto_save_chunk()

        if self._COMMIT_FLAG:
            self._COMMIT_FLAG = False

        del temp_vectors, temp_indices, temp_fields

        return new_ids

    def add_item(self, vector, index: int = None, field: str = None, normalize: bool = False,
                 save_immediately=False) -> int:
        """
        Add a single vector to the database.

        Parameters:
            vector (np.ndarray): The vector to be added.
            index (int, optional): Optional ID for the vector.
            field (str, optional): Optional field for the vector.
            normalize (bool): Whether to normalize the input vector.
            save_immediately (bool): Whether to save the database immediately.
        Returns:
            int: The ID of the added vector.

        Raises:
            ValueError: If the vector dimensions don't match or the ID already exists.
        """
        raise_if(ValueError, len(vector) != self.dim,
                 f'vector dim error, expect {self.dim}, got {len(vector)}')
        raise_if(ValueError, index in self.id_filter, f'id {index} already exists')
        raise_if(ValueError, vector.ndim != 1, f'vector dim error, expect 1, got {vector.ndim}')
        raise_if(ValueError, field is not None and not isinstance(field, str),
                 f'field must be str, got {type(field)}')
        raise_if(ValueError, index is not None and not isinstance(index, int), f'id must be int, got {type(index)}')
        raise_if(ValueError, field is not None and not isinstance(field, str),
                 f'field must be str, got {type(field)}')
        raise_if(ValueError, not isinstance(normalize, bool),
                 f'normalize must be bool, got {type(normalize)}')
        raise_if(ValueError, not isinstance(save_immediately, bool),
                 f'save_immediately must be bool, got {type(save_immediately)}')

        index = self._generate_new_id() if index is None else index

        vector, index, field = self._process_vector_item(vector, index, field, normalize)

        # Add the id to then bloom filter.
        self.id_filter.add(index)

        self.database.append(vector)
        self.indices.append(index)
        self.fields.append(field)

        if save_immediately:
            self.save_chunk_immediately()
        else:
            self.auto_save_chunk()

        if self._COMMIT_FLAG:
            self._COMMIT_FLAG = False

        return index

    def delete(self):
        """Delete all .mvdb files in the database_chunk_path list and reset the database."""
        if len(self.database_cluster_path) == 0 and len(self.database_chunk_path) == 0:
            return None

        try:
            shutil.rmtree(self.database_path_parent)
        except FileNotFoundError:
            pass

        self.reset_database()

    def _kmeans_clustering(self, data):

        self.ann_model = self.ann_model.partial_fit(data)

        return self.ann_model.cluster_centers_, self.ann_model.labels_

    def _predict_next_cluster_file_path(self, cluster_id):
        """预估下一个聚类文件的路径"""
        # {cluster_id: {file_path1: length, file_path2: length}}
        # find max file path
        if not self.current_cluster_data_msg.get(cluster_id):
            self.current_cluster_data_msg[cluster_id] = dict()
            max_cluster_id_path = self.database_path_parent / f'{self.database_name_prefix}_cluster_{cluster_id}_0.mvdb'
            self.current_cluster_data_msg[cluster_id][max_cluster_id_path] = 1
        else:
            max_cluster_id_path = max(self.current_cluster_data_msg[cluster_id].keys(),
                                      key=lambda x: int(str(x).split('.')[0].split('_')[-1]))
            max_id = int(str(max_cluster_id_path).split('.')[0].split('_')[-1])
            max_cluster_id_length = self.current_cluster_data_msg[cluster_id][max_cluster_id_path]

            if max_cluster_id_length + 1 > self.chunk_size:
                max_cluster_id_path = (self.database_path_parent /
                                       f'{self.database_name_prefix}_cluster_{cluster_id}_{max_id + 1}.mvdb')
                self.current_cluster_data_msg[cluster_id][max_cluster_id_path] = 0

            self.current_cluster_data_msg[cluster_id][max_cluster_id_path] += 1

        return str(max_cluster_id_path)

    def _insert_ivf_index(self, data, indices, fields):
        # 执行K-means聚类
        self.cluster_centers, labels = self._kmeans_clustering(data)

        # 当前批次的ivf索引
        ivf_index = {i: [] for i in range(self.n_clusters)}
        # 将数据分配到各个聚类中心
        for idx, label in enumerate(labels):
            self.ivf_index.add_to_cluster(label, indices[idx], fields[idx],
                                          self._predict_next_cluster_file_path(label))
            ivf_index[label].append([idx, indices[idx], fields[idx]])

        return ivf_index

    def _build_index(self):
        _database = []
        for database, indices, fields in self.data_loader(self.database_chunk_path):
            # 创建IVF索引
            ivf_index = self._insert_ivf_index(database, indices, fields)
            # 重排文件
            self.reorganize_files(database, ivf_index)
            if len(database) > 100:
                random_length = int(len(database) * 0.1)
            elif 10 < len(database) <= 100:
                random_length = 10
            else:
                random_length = 1

            # 随机生成一部分数据索引
            random_indices = np.random.choice(len(database), size=random_length, replace=False)

            _database.append(database[random_indices, :])

        return np.vstack(_database)

    def build_index(self):
        """
        Build the IVF index.

        """
        sample_data = self._build_index()
        is_quality_good = self.evaluate_clustering(sample_data)

        if not is_quality_good:
            logger.print('The clustering quality is not good, reindexing...')
            if self.database_cluster_path:
                max_chunk_id = max([int(Path(i).name.split('.')[0].split('_')[-1]) for i in self.database_chunk_path
                                    if Path(i).name.split('.')[0].split('_')[-1].isdigit()])
            else:
                max_chunk_id = 0

            for i in self.database_cluster_path:
                os.rename(i, self.database_path_parent / f'{self.database_name_prefix}_chunk_{max_chunk_id + 1}.mvdb')
                max_chunk_id += 1

            self.database_cluster_path = []
            # reset cluster meta data
            self.current_cluster_data_msg = dict()

            self.reindex_if_appropriate()

    def evaluate_clustering(self, data):
        """
        使用轮廓系数评估聚类质量
        """
        threshold = float(os.environ.get("MVDB_CLUSTERING_QUALITY_THRESHOLD", "0.3"))

        labels = self.ann_model.predict(data)
        score = silhouette_score(data, labels)
        logger.print(f"The clustering quality is: {score}")
        return score > threshold

    def reindex_if_appropriate(self):
        """
        如果聚类质量不好，则重新建立索引
        """
        self.ann_model = KMeans(n_clusters=self.n_clusters, random_state=0,
                                batch_size=10240, device=self.device)

        self.ivf_index = CompactIVFIndex(n_clusters=self.n_clusters)
        self._build_index()

    def reorganize_files(self, database, ivf_index):
        """
        根据IVF索引重排文件。
        """
        for cluster_id in range(self.n_clusters):
            all_vectors, all_indices, all_fields = [], [], []

            for cid, _ in ivf_index.items():
                if len(_) == 0:
                    continue
                vec_idx, index, field = zip(*_)
                if cid == cluster_id:
                    # 提取当前聚类中心的所有向量、索引和字段
                    all_vectors.extend([database[i] for i in vec_idx])
                    all_indices.extend(index)
                    all_fields.extend(field)

            if len(all_vectors) == 0:
                continue

            self._load_and_merge_cluster_file(cluster_id, all_vectors, all_indices, all_fields)

    @io_checker
    def _load_and_merge_cluster_file(self, cluster_id, database, indices, fields):
        """
        从已有的.mvdb文件中加载数据，然后混合新数据重新写入文件。
        """
        paths = self.save_and_merge_data(
            f'cluster_{cluster_id}',
            database,
            indices,
            fields,
            self.database_cluster_path
        )

        for i in paths:
            self._append_to_list_without_conflict(i, self.database_cluster_path)

        return paths

    @property
    def shape(self):
        """
        Return the shape of the entire database.

        Returns:
            tuple: The number of vectors and the dimension of each vector in the database.
        """
        self.check_commit()

        return self.database_shape